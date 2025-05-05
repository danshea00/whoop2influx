import os
import json
import getpass
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin
from authlib.common.urls import extract_params
from authlib.integrations.requests_client import OAuth2Session, OAuthError
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv()

AUTH_TOKEN_URL = "https://api-7.whoop.com/oauth/token"
API_BASE_URL = "https://api.prod.whoop.com"
API_DEVELOPER_PATH = "/developer"
WHOOP_USERNAME = os.getenv("WHOOP_USERNAME")
WHOOP_PASSWORD = os.getenv("WHOOP_PASSWORD")

INFLUXDB_URL = os.getenv("INFLUXDB_URL")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")

TOKEN_ENDPOINT_AUTH_METHOD = "password_json"
def _auth_password_json(_client, _method, uri, headers, body):
    body_params = dict(extract_params(body))
    if 'grant_type' not in body_params: body_params['grant_type'] = 'password'
    body = json.dumps(body_params)
    headers["Content-Type"] = "application/json"
    return uri, headers, body

def _handle_api_error(error: Exception, context: str):
    print(f"\nError during {context}: {error}")
    response = getattr(error, 'response', None)
    if response is not None:
        try:
            print(f"Status Code: {response.status_code}")
            print(f"Response Text: {response.text}")
        except Exception as e:
            print(f"Could not parse error response details: {e}")

def _make_request(session: OAuth2Session, method: str, url_slug: str, **kwargs) -> Optional[Dict[str, Any]]:
    full_url = urljoin(f"{API_BASE_URL}{API_DEVELOPER_PATH}/", url_slug)
    try:
        response = session.request(method, full_url, **kwargs)
        response.raise_for_status()
        if response.status_code == 204: return {"status": "success", "status_code": 204}
        return response.json()
    except Exception as e:
        _handle_api_error(e, f"{method} request to {full_url}")
        return None

def _make_paginated_request(session: OAuth2Session, method: str, url_slug: str, **kwargs) -> List[Dict[str, Any]]:
    all_records = []
    params = kwargs.pop("params", {})
    next_token = params.pop("nextToken", None)
    page_count = 0
    max_pages = 10

    while page_count < max_pages:
        page_count += 1
        current_params = params.copy()
        if next_token:
            current_params["nextToken"] = next_token

        response_data = _make_request(session, method, url_slug, params=current_params, **kwargs)

        if response_data is None:
            print(f"Paginated request failed on page {page_count}.")
            break

        records = response_data.get("records", [])
        all_records.extend(records)

        next_token = response_data.get("next_token")
        if not next_token:
            break

    if page_count >= max_pages:
         print(f"Warning: Reached max page limit ({max_pages}) for {url_slug}. Data might be incomplete.")

    return all_records

def authenticate_session() -> Optional[OAuth2Session]:
    global WHOOP_PASSWORD
    if not WHOOP_USERNAME:
        print("Error: WHOOP_USERNAME not set.")
        return None
    if not WHOOP_PASSWORD:
        print(f"WHOOP_PASSWORD environment variable not set.")
        try: WHOOP_PASSWORD = getpass.getpass(f"Enter Whoop password for {WHOOP_USERNAME}: ")
        except Exception as e: print(f"\nError reading password: {e}"); return None
        if not WHOOP_PASSWORD: print("\nPassword cannot be empty."); return None

    session = OAuth2Session(token_endpoint_auth_method=TOKEN_ENDPOINT_AUTH_METHOD)
    session.register_client_auth_method((TOKEN_ENDPOINT_AUTH_METHOD, _auth_password_json))
    try:
        token_data = session.fetch_token(
            url=AUTH_TOKEN_URL, username=WHOOP_USERNAME, password=WHOOP_PASSWORD, grant_type="password"
        )
        print("\nAuthentication Successful!")
        user_id = token_data.get('user', {}).get('id')
        print(f"  User ID: {user_id if user_id else 'N/A'}")

        user_profile_data = token_data.get('user', {}).get('profile', {})
        if user_profile_data and user_id:
             user_profile_data['user_id'] = user_id
             session.profile_data = user_profile_data
        else:
             session.profile_data = None
             print("Warning: Profile data not found in authentication response.")

        return session
    except Exception as e:
        _handle_api_error(e, f"authentication for {WHOOP_USERNAME}")
        session.close()
        return None

def create_influx_point(measurement: str, time_str: str, fields: Dict[str, Any], tags: Optional[Dict[str, str]] = None) -> Optional[Point]:
    """Creates an InfluxDB Point object, handling potential errors."""
    try:
        timestamp = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
        point = Point(measurement).time(timestamp, WritePrecision.S)

        if tags:
            for key, value in tags.items():
                if value is not None:
                    point.tag(key, str(value))

        valid_fields = False
        for key, value in fields.items():
            if isinstance(value, (int, float, bool, str)):
                point.field(key, value)
                valid_fields = True
            elif value is not None:
                 print(f"Warning: Skipping field '{key}' in '{measurement}' due to unsupported type {type(value)} (value: {value})")

        return point if valid_fields else None
    except ValueError as e:
        print(f"Error parsing timestamp '{time_str}' for measurement '{measurement}': {e}")
        return None
    except Exception as e:
        print(f"Error creating InfluxDB point for measurement '{measurement}': {e}")
        return None

def process_cycles(session: OAuth2Session, lookback_days: int = 1) -> List[Point]:
    """Fetches recent cycles and formats them for InfluxDB."""
    print("\n--- Processing Cycles ---")
    points = []
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=lookback_days)
    cycles = get_cycle_collection(session, start=start_date.isoformat(), end=end_date.isoformat(), limit=25)

    for cycle in cycles:
        if cycle.get("score_state") == "SCORED" and cycle.get("score"):
            fields = {
                "strain": cycle["score"].get("strain"),
                "kilojoule": cycle["score"].get("kilojoule"),
                "average_heart_rate": cycle["score"].get("average_heart_rate"),
                "max_heart_rate": cycle["score"].get("max_heart_rate"),
            }
            valid_fields = {k: v for k, v in fields.items() if v is not None}
            if valid_fields:
                point = create_influx_point(
                    measurement="whoop_cycle",
                    time_str=cycle["start"],
                    fields=valid_fields,
                    tags={"user_id": str(cycle.get("user_id")), "cycle_id": str(cycle.get("id"))}
                )
                if point: points.append(point)
    print(f"Formatted {len(points)} cycle points.")
    return points

def process_recoveries(session: OAuth2Session, lookback_days: int = 1) -> List[Point]:
    """Fetches recent recoveries and formats them for InfluxDB."""
    print("\n--- Processing Recoveries ---")
    points = []
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=lookback_days)
    recoveries = get_recovery_collection(session, start=start_date.isoformat(), end=end_date.isoformat(), limit=25)

    for recovery in recoveries:
         if recovery.get("score_state") == "SCORED" and recovery.get("score"):
            fields = {
                "recovery_score": recovery["score"].get("recovery_score"),
                "resting_heart_rate": recovery["score"].get("resting_heart_rate"),
                "hrv_rmssd_milli": recovery["score"].get("hrv_rmssd_milli"),
                "spo2_percentage": recovery["score"].get("spo2_percentage"),
                "skin_temp_celsius": recovery["score"].get("skin_temp_celsius"),
                "user_calibrating": recovery["score"].get("user_calibrating"),
            }
            valid_fields = {k: v for k, v in fields.items() if v is not None}
            if valid_fields:
                point = create_influx_point(
                    measurement="whoop_recovery",
                    time_str=recovery["updated_at"],
                    fields=valid_fields,
                    tags={"user_id": str(recovery.get("user_id")), "cycle_id": str(recovery.get("cycle_id")), "sleep_id": str(recovery.get("sleep_id"))}
                )
                if point: points.append(point)
    print(f"Formatted {len(points)} recovery points.")
    return points

def process_sleeps(session: OAuth2Session, lookback_days: int = 1) -> List[Point]:
    """Fetches recent sleeps and formats them for InfluxDB."""
    print("\n--- Processing Sleeps ---")
    points = []
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=lookback_days)
    sleeps = get_sleep_collection(session, start=start_date.isoformat(), end=end_date.isoformat(), limit=25)

    for sleep in sleeps:
        if sleep.get("score_state") == "SCORED" and sleep.get("score"):
            score_fields = {
                "respiratory_rate": sleep["score"].get("respiratory_rate"),
                "sleep_performance_percentage": sleep["score"].get("sleep_performance_percentage"),
                "sleep_consistency_percentage": sleep["score"].get("sleep_consistency_percentage"),
                "sleep_efficiency_percentage": sleep["score"].get("sleep_efficiency_percentage"),
            }
            stage_summary = sleep["score"].get("stage_summary", {})
            stage_fields = {
                "total_in_bed_time_milli": stage_summary.get("total_in_bed_time_milli"),
                "total_awake_time_milli": stage_summary.get("total_awake_time_milli"),
                "total_no_data_time_milli": stage_summary.get("total_no_data_time_milli"),
                "total_light_sleep_time_milli": stage_summary.get("total_light_sleep_time_milli"),
                "total_slow_wave_sleep_time_milli": stage_summary.get("total_slow_wave_sleep_time_milli"),
                "total_rem_sleep_time_milli": stage_summary.get("total_rem_sleep_time_milli"),
                "sleep_cycle_count": stage_summary.get("sleep_cycle_count"),
                "disturbance_count": stage_summary.get("disturbance_count"),
            }
            needed_summary = sleep["score"].get("sleep_needed", {})
            needed_fields = {
                 "needed_baseline_milli": needed_summary.get("baseline_milli"),
                 "needed_sleep_debt_milli": needed_summary.get("need_from_sleep_debt_milli"),
                 "needed_recent_strain_milli": needed_summary.get("need_from_recent_strain_milli"),
                 "needed_recent_nap_milli": needed_summary.get("need_from_recent_nap_milli"),
            }

            all_fields = {**score_fields, **stage_fields, **needed_fields}
            valid_fields = {k: v for k, v in all_fields.items() if v is not None}

            if valid_fields:
                point = create_influx_point(
                    measurement="whoop_sleep",
                    time_str=sleep["start"],
                    fields=valid_fields,
                    tags={"user_id": str(sleep.get("user_id")), "sleep_id": str(sleep.get("id")), "is_nap": str(sleep.get("nap", False))}
                )
                if point: points.append(point)
    print(f"Formatted {len(points)} sleep points.")
    return points

def process_workouts(session: OAuth2Session, lookback_days: int = 1) -> List[Point]:
    """Fetches recent workouts and formats them for InfluxDB."""
    print("\n--- Processing Workouts ---")
    points = []
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=lookback_days)
    workouts = get_workout_collection(session, start=start_date.isoformat(), end=end_date.isoformat(), limit=25)

    for workout in workouts:
         if workout.get("score_state") == "SCORED" and workout.get("score"):
            score_fields = {
                "strain": workout["score"].get("strain"),
                "average_heart_rate": workout["score"].get("average_heart_rate"),
                "max_heart_rate": workout["score"].get("max_heart_rate"),
                "kilojoule": workout["score"].get("kilojoule"),
                "percent_recorded": workout["score"].get("percent_recorded"),
                "distance_meter": workout["score"].get("distance_meter"),
                "altitude_gain_meter": workout["score"].get("altitude_gain_meter"),
                "altitude_change_meter": workout["score"].get("altitude_change_meter"),
            }
            zone_summary = workout["score"].get("zone_duration", {})
            zone_fields = {
                "zone0_milli": zone_summary.get("zone_zero_milli"),
                "zone1_milli": zone_summary.get("zone_one_milli"),
                "zone2_milli": zone_summary.get("zone_two_milli"),
                "zone3_milli": zone_summary.get("zone_three_milli"),
                "zone4_milli": zone_summary.get("zone_four_milli"),
                "zone5_milli": zone_summary.get("zone_five_milli"),
            }
            all_fields = {**score_fields, **zone_fields}
            valid_fields = {k: v for k, v in all_fields.items() if v is not None}

            if valid_fields:
                 point = create_influx_point(
                    measurement="whoop_workout",
                    time_str=workout["start"],
                    fields=valid_fields,
                    tags={"user_id": str(workout.get("user_id")), "workout_id": str(workout.get("id")), "sport_id": str(workout.get("sport_id"))}
                )
                 if point: points.append(point)
    print(f"Formatted {len(points)} workout points.")
    return points

def get_cycle_collection(session: OAuth2Session, start: Optional[str] = None, end: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    params = {"limit": min(limit, 25)}
    if start: params["start"] = start
    if end: params["end"] = end
    return _make_paginated_request(session, "GET", "v1/cycle", params=params)

def get_recovery_collection(session: OAuth2Session, start: Optional[str] = None, end: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    params = {"limit": min(limit, 25)}
    if start: params["start"] = start
    if end: params["end"] = end
    return _make_paginated_request(session, "GET", "v1/recovery", params=params)

def get_sleep_collection(session: OAuth2Session, start: Optional[str] = None, end: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    params = {"limit": min(limit, 25)}
    if start: params["start"] = start
    if end: params["end"] = end
    return _make_paginated_request(session, "GET", "v1/activity/sleep", params=params)

def get_workout_collection(session: OAuth2Session, start: Optional[str] = None, end: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    params = {"limit": min(limit, 25)}
    if start: params["start"] = start
    if end: params["end"] = end
    return _make_paginated_request(session, "GET", "v1/activity/workout", params=params)

if __name__ == "__main__":
    print("Starting Whoop data export to InfluxDB...")

    if not all([INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET]):
        print("Error: InfluxDB environment variables (INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET) are not fully set.")
        sys.exit(1)
    if INFLUXDB_TOKEN == 'YOUR_INFLUXDB_API_TOKEN':
         print("Error: Please replace 'YOUR_INFLUXDB_API_TOKEN' in your .env file with your actual InfluxDB token.")
         sys.exit(1)

    auth_session = authenticate_session()
    if not auth_session:
        print("\nCould not establish authenticated Whoop session. Exiting.")
        sys.exit(1)

    print(f"\nConnecting to InfluxDB: URL={INFLUXDB_URL}, Org={INFLUXDB_ORG}, Bucket={INFLUXDB_BUCKET}")
    try:
        influx_client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
        write_api = influx_client.write_api(write_options=SYNCHRONOUS)
        influx_client.ping()
        print("InfluxDB connection successful.")
    except Exception as e:
        print(f"Error connecting to InfluxDB: {e}")
        auth_session.close()
        sys.exit(1)

    profile_points = []
    if hasattr(auth_session, 'profile_data') and auth_session.profile_data:
        print("\n--- Processing Profile Data ---")
        profile_data = auth_session.profile_data
        profile_fields = {
            "height_meter": profile_data.get("height"),
            "weight_kilogram": profile_data.get("weight"),
            "max_heart_rate": profile_data.get("maxHeartRate"),
            "min_heart_rate": profile_data.get("minHeartRate"),
            "fitness_level": profile_data.get("fitnessLevel"), # Add fitness level
        }
        valid_profile_fields = {k: v for k, v in profile_fields.items() if v is not None}
        if valid_profile_fields:
            profile_point = create_influx_point(
                measurement="whoop_profile",
                time_str=datetime.now(timezone.utc).isoformat(),
                fields=valid_profile_fields,
                tags={"user_id": str(profile_data.get("user_id"))}
            )
            if profile_point:
                profile_points.append(profile_point)
                print(f"Formatted {len(profile_points)} profile point.")

    total_points_written = 0

    if profile_points:
        print(f"Writing {len(profile_points)} profile points...")
        try:
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=profile_points)
            total_points_written += len(profile_points)
            print("Successfully wrote profile points.")
        except Exception as e:
            print(f"Error writing profile points to InfluxDB: {e}")

    lookback = 2

    cycle_points = process_cycles(auth_session, lookback_days=lookback)
    if cycle_points:
        print(f"Writing {len(cycle_points)} cycle points...")
        try:
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=cycle_points)
            total_points_written += len(cycle_points)
            print("Successfully wrote cycle points.")
        except Exception as e:
            print(f"Error writing cycle points to InfluxDB: {e}")

    recovery_points = process_recoveries(auth_session, lookback_days=lookback)
    if recovery_points:
        print(f"Writing {len(recovery_points)} recovery points...")
        try:
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=recovery_points)
            total_points_written += len(recovery_points)
            print("Successfully wrote recovery points.")
        except Exception as e:
            print(f"Error writing recovery points to InfluxDB: {e}")

    sleep_points = process_sleeps(auth_session, lookback_days=lookback)
    if sleep_points:
        print(f"Writing {len(sleep_points)} sleep points...")
        try:
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=sleep_points)
            total_points_written += len(sleep_points)
            print("Successfully wrote sleep points.")
        except Exception as e:
            print(f"Error writing sleep points to InfluxDB: {e}")

    workout_points = process_workouts(auth_session, lookback_days=lookback)
    if workout_points:
        print(f"Writing {len(workout_points)} workout points...")
        try:
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=workout_points)
            total_points_written += len(workout_points)
            print("Successfully wrote workout points.")
        except Exception as e:
            print(f"Error writing workout points to InfluxDB: {e}")


    print(f"\nTotal points written in this run: {total_points_written}")

    auth_session.close()
    write_api.close()
    influx_client.close()
    print("\nWhoop session and InfluxDB connection closed.")
    print("Script finished.")