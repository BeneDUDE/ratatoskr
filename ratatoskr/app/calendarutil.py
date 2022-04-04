import base64
import datetime
from threading import Thread
from ratatoskr.threadutil import daemon
import ratatoskr.settings
import hashlib
from django.utils.timezone import make_aware
from pydoc import cli
import uuid
import pytz
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from allauth.socialaccount.models import SocialToken, SocialApp
from django.contrib.auth.models import User
from numpy import byte
from ratelimit import limits, sleep_and_retry

# Notes:
# Client object is just a capsule for the Credentials, there is no cost to building multiple client objects

# The IDs we generate for calendar events are created in a way that it links to their timeslot
# So in order to get the event associated with a timeslot, all we need is the timeslot's data, and we don't need to store the ID on the database

# Effectively we construct a string that identifies the timeslot and pass it through SHA-1 to reduce the chance of collision and...
# comply with the base32 rule for event ids

if ratatoskr.settings.DEBUG:
    CALENDAR_ID_SUFFIX = "debug.meetings.techhigh.us"
else:
    CALENDAR_ID_SUFFIX = "meetings.techhigh.us"

CALENDAR_TIMESLOT_EVENT_ID = "%(timeslot_id)s@%(schedule_id)s#" + CALENDAR_ID_SUFFIX

# 9 calls per second
api_limits_decorator = limits(calls=9, period=1)

# Decorator function for rate limiting async calls to a function
def api_pool(func):
    @daemon
    @sleep_and_retry
    @api_limits_decorator
    def inner(*args, **kwargs):
        func(*args, **kwargs)
    return inner

def hashify(string: str) -> str:
    return hashlib.sha1(bytes(string, "ascii")).hexdigest().lower()


def build_timeslot_event_id(timeslot) -> str:
    built_string = CALENDAR_TIMESLOT_EVENT_ID % {
        "schedule_id": timeslot.schedule.id,
        "timeslot_id": timeslot.id
    }
    return hashify(built_string)


# Builds the calendar api using the User's api tokens
def build_calendar_client(user):
    token = SocialToken.objects.get(account__user=user, account__provider='google')
    google_app = SocialApp.objects.get(provider="google")
    credentials = Credentials(
        token=token.token,
        refresh_token=token.token_secret,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=google_app.client_id,
        client_secret=google_app.secret)
    return build('calendar', 'v3', credentials=credentials)


# Creates a calendar for the given schedule
# Returns conferenceData and calendarId to be saved in the schedule model
def create_calendar_for_schedule(schedule) -> tuple[dict, str]:
    client = build_calendar_client(schedule.owner)
    calendar_body = {
        'summary': f'Ratatoskr: {schedule.name}',
        'description': 'Calendar generated by Ratatoskr. Please do not delete.',
        'timeZone': 'UTC',
        'conferenceProperties': {
            "allowedConferenceSolutionTypes": [
                "hangoutsMeet"
            ]
        }
    }
    dummy_event_body = {
        "summary": "Ratatoskr Dummy Event",
        "location": "Yggdrasil",
        "description": "This event was only supposed to exist for a short time. If this event happened to stay, "
                       "you are free to delete it.",
        "start": {
            "dateTime": make_aware(datetime.datetime.now()).isoformat(),
        },
        "end": {
            "dateTime": (make_aware(datetime.datetime.now()) + datetime.timedelta(hours=2)).isoformat(),
        },
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                # Comment out the line below for testing with a student account.
                # Students cannot create Google Meets, therefore this returns an error from the API.
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "attendees": [],
        "reminders": {"useDefault": False},
    }
    calendar = client.calendars().insert(body=calendar_body).execute()
    calendar_id = calendar['id']
    # We are going to create a dummy event to get some conference data to use with other events on the same calendar
    event = client.events().insert(calendarId=calendar_id, conferenceDataVersion=1, body=dummy_event_body).execute()

    # Commenting out the line below and uncommenting conf_data = {} will allow student accounts to use for testing.
    conf_data = event["conferenceData"]
    # conf_data = {}

    # Delete the dummy event, we don't need it
    client.events().delete(calendarId=calendar_id, eventId=event["id"]).execute()
    return conf_data, calendar_id

@api_pool
def delete_calendar_for_schedule(schedule) -> None:
    client = build_calendar_client(schedule.owner)
    client.calendars().delete(calendarId=schedule.calendar_id)

# Updates the calendar event associated with the timeslot
# If the event does not exist, this function will create one
@api_pool
def update_timeslot_event(timeslot) -> None:
    client = build_calendar_client(timeslot.schedule.owner)

    calendar_id = timeslot.schedule.calendar_id
    event_id = build_timeslot_event_id(timeslot)
    conf_data = timeslot.schedule.calendar_meet_data
    
    # Google Calendar does some implicit time conversions for DST that do not want to cooperate with the times we give it,
    # so we basically have to resort to this monkey buisness of naivifying these times then setting it back to EST to 
    # *somehow* make these times convert the correct way.
    # TODO: Abolish daylight savings time
    # UPDATE: Ladies and gentlemen, we got em: https://www.reuters.com/world/us/us-senate-approves-bill-that-would-make-daylight-savings-time-permanent-2023-2022-03-15/

    if timeslot.reservation_set.count() != 0:
        # Normal time if timeslot has reservations
        east = pytz.timezone("America/New_York")
        start = east.localize(timeslot.time_from.replace(tzinfo=None)).isoformat()
        end = east.localize(timeslot.time_to.replace(tzinfo=None)).isoformat()
    else:
        # Hide event if there are no reservations
        utc = pytz.timezone("UTC")
        start = utc.localize(datetime.datetime(1970, 1, 1)).isoformat()
        end = utc.localize(datetime.datetime(1970, 1, 1)).isoformat()

    event_body = {
        "summary": f"Ratatoskr: {timeslot.schedule.name}",
        "location": "Atop Yggdrasil",
        "description": "Event relayed to you by Ratatoskr 🐭.",
        "start": {
            "dateTime": start,
        },
        "end": {
            "dateTime": end,
        },
        "conferenceData": conf_data,
        "attendees": [
            {
                "email": r.email,
                "displayName": r.name,
                "comment": r.comment
            } for r in timeslot.reservation_set.filter(confirmed=True).all()
        ],
        "reminders": {
            "useDefault": False,
            "overrides": [
                {
                    "method": "email",
                    "minutes": 60 * 24 * 2  # Two days
                },
                {
                    "method": "email",
                    "minutes": 60 * 24  # One day
                },
                {
                    "method": "email",
                    "minutes": 10  # 10 minutes
                }
            ]
        },
        "id": event_id
    }
    # Just do this asyncrhonouslyk
    try:
        # Will fail with 404 if the event does not exist
        client.events().patch(calendarId=calendar_id, eventId=event_id, conferenceDataVersion=1,
                            body=event_body).execute()
    except HttpError:  # And if the event doesn't exist, we create a new event.
        client.events().insert(calendarId=calendar_id, conferenceDataVersion=1, body=event_body).execute()


# Deletes the event associated with the timeslot
@api_pool
def delete_timeslot_event(timeslot) -> None:
    client = build_calendar_client(timeslot.schedule.owner)
    eventid = build_timeslot_event_id(timeslot)
    client.events().delete(
        calendarId=timeslot.schedule.calendar_id,
        eventId=build_timeslot_event_id(timeslot)
    )
