
import base64
import datetime
import hashlib
from django.utils.timezone import make_aware
from pydoc import cli
import uuid
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from allauth.socialaccount.models import SocialToken, SocialApp
from django.contrib.auth.models import User
from numpy import byte

from .models import Reservation, Schedule, ScheduleMeetingData, TimeSlot

# Notes:
# Client object is just a capsule for the Credentials, there is no cost to building multiple client objects

# Template strings for the IDs for each of our calendar elements
# The name+ID method for generating IDs for our calendar elements should be unique enough so it doesn't clash with any other IDs
# AutoIncrement fields in SQL never return previous numbers, so we should also be safe in that regard too.

CALENDAR_ID_SUFFIX = "ratatoskr.techhigh.us"
CALENDAR_SCHEDULE_ID = "{schedule_id}" + CALENDAR_ID_SUFFIX
CALENDAR_TIMESLOT_EVENT_ID = "{timeslot_id}@{schedule_id}#" + CALENDAR_ID_SUFFIX

def hashify(string: str) -> str:
    return hashlib.sha1(bytes(string, "ascii")).hexdigest().lower()

def build_schedule_id(schedule: Schedule) -> str:
    built_string = CALENDAR_SCHEDULE_ID % {
        "schedule_id": schedule.id
    }
    return hashify(built_string)

def build_timeslot_event_id(timeslot: TimeSlot) -> str:
    built_string = CALENDAR_TIMESLOT_EVENT_ID % {
        "schedule_id": timeslot.schedule.id,
        "timeslot_id": timeslot.id
    }
    return hashify(built_string)

# Builds the calendar api using the User's api tokens
def build_calendar_client(user: User):
    token = SocialToken.objects.get(account__user=user, account__provider='google')
    google_app = SocialApp.objects.get(provider="google")
    credentials = Credentials(
        token=token.token,
        refresh_token=token.token_secret,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=google_app.client_id, 
        client_secret=google_app.secret) 
    return build('calendar', 'v3', credentials=credentials)

# Gets the calendar associated with the schedule
def create_calendar_for_schedule(schedule: Schedule) -> None:
    client = build_calendar_client(schedule.owner)
    calendar_body = {
        'summary': f'Ratatoskr: {schedule.name}',
        'description': 'Calendar generated by Ratatoskr. Please do not delete.',
        'timeZone': 'America/New_York',
        'conferenceProperties': ["hangoutsMeet"]
    }
    dummy_event_body = {
        "summary": "Ratatoskr Dummy Event",
        "location": "Yggdrasil",
        "description": "This event was only supposed to exist for a short time. If this event happened to stay, you are free to delete it.",
        "start": {
            "dateTime": make_aware(datetime.datetime.now()).isoformat(),
            "timeZone": "America/New_York",
        },
        "end": {
            "dateTime": (make_aware(datetime.datetime.now()) + datetime.timedelta(hours=2)).isoformat(),
            "timeZone": "America/New_York",
        },
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "attendees": [],
        "reminders": {"useDefault": False},
    }
    calendar = client.calendars().insert(body=calendar_body).execute()
    calendar_id = calendar['id']
    schedule.calendar_id = calendar_id
    schedule.save()
    # We are going to create a dummy event to get some conference data to use with other events on the same calendar
    event = client.events().insert(calendarId=calendar_id, conferenceDataVersion=1, body=dummy_event_body).execute()
    conf_data = event["conferenceData"]
    ScheduleMeetingData.objects.create(
        schedule=schedule,
        meet_data=conf_data
    )
    # Delete the dummy event, we don't need it
    client.events().delete(calendarId=calendar_id, eventId=event["id"]).execute()

def update_timeslot_events(timeslot: TimeSlot) -> None:
    client = build_calendar_client(timeslot.schedule.owner)
    calendar_id = timeslot.schedule.calendar_id
    event_id = build_timeslot_event_id(timeslot)
    conf_data = ScheduleMeetingData.objects.get(schedule=timeslot.schedule).meet_data
    event_body = {
        "summary": f"Ratatoskr: {timeslot.schedule.name}",
        "location": "Peak of Yggdrasil",
        "description": "Event manifested by the Ratatoskr Meeting System.",
        "start": {
            "dateTime": timeslot.time_from.isoformat(),
        },
        "end": {
            "dateTime": timeslot.time_to.isoformat(),
        },
        "conferenceData": conf_data,
        "attendees": [{"email": r.email} for r in timeslot.reservation_set.all()],
        "reminders": {"useDefault": True},
        "id": event_id
    }
    try:
        client.events().get(calendarId=calendar_id, eventId=event_id).execute()
        client.events().patch(calendarId=calendar_id, eventId=event_id, body=event_body).execute()
    except HttpError: # Aaand pray that we don't get a random timed-out error
        client.events().insert(calendarId=calendar_id, body=event_body).execute()
