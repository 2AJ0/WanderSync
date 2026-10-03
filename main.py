import os
import json
import httpx
import hashlib
import asyncio
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import math
import uuid
import database
import models
from sqlalchemy.orm import Session

load_dotenv()

models.Base.metadata.create_all(bind=database.engine)

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

GOOGLE_ROUTES_API_KEY = os.getenv("GOOGLE_ROUTES_API_KEY", "")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
USE_MOCK_API = False  # Enforcing Live API

class ChatRequest(BaseModel):
    message: str
    context: str = ""

app = FastAPI()

class RouteRequest(BaseModel):
    source: str
    destination: str
    stops: List[str] = []
    expressway_speed: float = 100.0
    highway_speed: float = 80.0
    main_road_speed: float = 50.0
    wheelchair: bool = False
    planner_mode: bool = False
    ai_planner: bool = False
    interests: str = ""
    allowed_modes: List[str] = ["DRIVE", "TRANSIT", "FLIGHT", "BICYCLE"]
    recommend_places: bool = False
    photos: bool = False
    detours: bool = False
    max_budget: Optional[float] = None
    duration_days: int = 3

@app.get("/api/config")
def get_config():
    return {
        "google_maps_api_key": GOOGLE_ROUTES_API_KEY,
        "gemini_enabled": bool(GEMINI_API_KEY)
    }

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict = {}

    async def connect(self, websocket: WebSocket, trip_id: str):
        await websocket.accept()
        if trip_id not in self.active_connections:
            self.active_connections[trip_id] = []
        self.active_connections[trip_id].append(websocket)

    def disconnect(self, websocket: WebSocket, trip_id: str):
        if trip_id in self.active_connections:
            self.active_connections[trip_id].remove(websocket)

    async def broadcast(self, message: str, trip_id: str):
        if trip_id in self.active_connections:
            for connection in self.active_connections[trip_id]:
                try:
                    await connection.send_text(message)
                except:
                    pass

manager = ConnectionManager()

# In-memory Caches
ROUTE_CACHE = {}
PLACES_CACHE = {}
ITINERARY_CACHE = {}

def get_cache_key(*args, **kwargs):
    key_str = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True)
    return hashlib.md5(key_str.encode()).hexdigest()

def haversine(coord1, coord2):
    """Calculate distance in km between two lat/lng pairs."""
    lat1, lon1 = coord1
    lat2, lon2 = coord2
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

async def get_coordinates(location: str):
    """Uses Google Places API to get coordinates."""
    if not GOOGLE_ROUTES_API_KEY:
        return None
    url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={location}&key={GOOGLE_ROUTES_API_KEY}"
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, timeout=5.0)
            data = res.json()
            if data.get("results") and len(data["results"]) > 0:
                loc = data["results"][0]["geometry"]["location"]
                return [loc["lat"], loc["lng"]]
        except:
            pass
    return None

async def fetch_weather(lat: float, lng: float):
    """Fetches weather from open-meteo."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lng}&current_weather=true"
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, timeout=5.0)
            data = res.json()
            if "current_weather" in data:
                cw = data["current_weather"]
                return {
                    "temperature": cw.get("temperature"),
                    "windspeed": cw.get("windspeed"),
                    "weathercode": cw.get("weathercode")
                }
        except:
            pass
    return None

async def fetch_places_for_itinerary(destination: str, interests: str, get_photos: bool):
    """Uses Google Places API textsearch to get places with photos/ratings."""
    cache_key = get_cache_key("google_places", destination, interests, get_photos)
    if cache_key in PLACES_CACHE:
        return PLACES_CACHE[cache_key]
        
    if not GOOGLE_ROUTES_API_KEY:
        return []
    query = f"{interests} in {destination}" if interests else f"tourist attractions in {destination}"
    url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={query}&key={GOOGLE_ROUTES_API_KEY}"
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, timeout=5.0)
            data = res.json()
            results = data.get("results", [])[:5]
            places = []
            for r in results:
                place = {
                    "name": r.get("name"),
                    "rating": r.get("rating"),
                    "user_ratings_total": r.get("user_ratings_total")
                }
                if get_photos and r.get("photos"):
                    photo_ref = r["photos"][0]["photo_reference"]
                    place["photo_url"] = f"https://maps.googleapis.com/maps/api/place/photo?maxwidth=400&photo_reference={photo_ref}&key={GOOGLE_ROUTES_API_KEY}"
                places.append(place)
            PLACES_CACHE[cache_key] = places
            return places
        except:
            return []

async def fetch_detours(midpoint_lat: float, midpoint_lng: float, get_photos: bool):
    """Fetches highly rated places near a coordinate."""
    if not GOOGLE_ROUTES_API_KEY:
        return []
    url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?location={midpoint_lat},{midpoint_lng}&radius=5000&type=tourist_attraction&key={GOOGLE_ROUTES_API_KEY}"
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, timeout=5.0)
            data = res.json()
            results = data.get("results", [])[:3]
            places = []
            for r in results:
                place = {
                    "name": r.get("name"),
                    "rating": r.get("rating"),
                    "user_ratings_total": r.get("user_ratings_total")
                }
                if get_photos and r.get("photos"):
                    photo_ref = r["photos"][0]["photo_reference"]
                    place["photo_url"] = f"https://maps.googleapis.com/maps/api/place/photo?maxwidth=400&photo_reference={photo_ref}&key={GOOGLE_ROUTES_API_KEY}"
                places.append(place)
            return places
        except:
            return []

async def fetch_google_routes(origin: str, destination: str, mode: str, waypoints: List[str] = [], wheelchair: bool = False):
    """Fetches overland routes using Google Routes API."""
    cache_key = get_cache_key("google_routes", origin, destination, mode, waypoints, wheelchair)
    if cache_key in ROUTE_CACHE:
        return ROUTE_CACHE[cache_key]

    if not GOOGLE_ROUTES_API_KEY:
        return []
        
    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "X-Goog-Api-Key": GOOGLE_ROUTES_API_KEY,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.description,routes.routeLabels,routes.polyline.encodedPolyline,routes.travelAdvisory,routes.legs",
        "Content-Type": "application/json"
    }
    
    payload = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": mode
    }
    
    if waypoints and mode != "TRANSIT":
        payload["intermediates"] = [{"address": wp} for wp in waypoints]
    
    if mode == "TRANSIT" and wheelchair:
        payload["transitPreferences"] = {"routingPreference": "WHEELCHAIR_ACCESSIBLE"}
    
    if mode == "DRIVE":
        payload["routingPreference"] = "TRAFFIC_AWARE"
        
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=payload, timeout=10.0)
            if response.status_code != 200:
                return []
            routes = response.json().get("routes", [])
            for r in routes:
                r["mode"] = mode
            ROUTE_CACHE[cache_key] = routes
            return routes
        except Exception:
            return []

async def fetch_airport_for_location(location: str):
    """Uses Google Places API to find the nearest international airport."""
    if not GOOGLE_ROUTES_API_KEY:
        return f"Airport near {location}", None
        
    url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query=international+airport+near+{location}&key={GOOGLE_ROUTES_API_KEY}"
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, timeout=5.0)
            data = res.json()
            if data.get("results") and len(data["results"]) > 0:
                airport_name = data["results"][0]["name"]
                loc = data["results"][0]["geometry"]["location"]
                return airport_name, [loc["lat"], loc["lng"]]
            return f"Airport near {location}", None
        except Exception:
            return f"Airport near {location}", None

from datetime import datetime, timedelta

async def fetch_serpapi_flights(origin_city: str, dest_city: str, origin_coords=None, dest_coords=None):
    """Returns a list of flight segments (handling layovers)."""
    if SERPAPI_API_KEY:
        outbound = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
        return_dt = (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d")
        url = f"https://serpapi.com/search.json?engine=google_flights&departure_id={origin_city}&arrival_id={dest_city}&type=2&outbound_date={outbound}&return_date={return_dt}&currency=USD&hl=en&api_key={SERPAPI_API_KEY}"
        async with httpx.AsyncClient() as client:
            try:
                res = await client.get(url, timeout=10.0)
                data = res.json()
                if "best_flights" in data and len(data["best_flights"]) > 0:
                    flight = data["best_flights"][0]
                    flights_arr = flight.get("flights", [])
                    
                    if len(flights_arr) > 1:
                        # Multiple flights (Layover)
                        legs = []
                        for idx, f in enumerate(flights_arr):
                            dep = f.get("departure_airport", {}).get("name", "Unknown Airport")
                            arr = f.get("arrival_airport", {}).get("name", "Unknown Airport")
                            dur_mins = f.get("duration", 120)
                            legs.append({
                                "mode": "FLIGHT",
                                "custom_name": f"{dep} -> {arr}",
                                "duration": f"{dur_mins * 60}s",
                                "distanceMeters": dur_mins * 60 * 250,
                                "polyline": {"encodedPolyline": ""},
                                "start": origin_coords if idx == 0 else None,
                                "end": dest_coords if idx == 0 else None
                            })
                        return legs
                    else:
                        # Single flight
                        duration_mins = flight.get("duration", 600)
                        return [{
                            "mode": "FLIGHT",
                            "duration": f"{duration_mins * 60}s",
                            "distanceMeters": duration_mins * 60 * 250,
                            "polyline": {"encodedPolyline": ""},
                            "start": origin_coords,
                            "end": dest_coords
                        }]
            except Exception:
                pass
                
    distance_km = 8000
    if origin_coords and dest_coords:
        distance_km = haversine(origin_coords, dest_coords)
        
    duration_hrs = distance_km / 800
    
    # Simulate a connecting flight if the distance is very large (>10,000 km) and no API is used
    if distance_km > 10000:
        return [
            {
                "mode": "FLIGHT",
                "custom_name": f"{origin_city} -> Midway Airport (Layover)",
                "duration": f"{int((duration_hrs/2) * 3600)}s",
                "distanceMeters": int((distance_km/2) * 1000),
                "polyline": {"encodedPolyline": ""},
                "start": origin_coords,
                "end": dest_coords
            },
            {
                "mode": "FLIGHT",
                "custom_name": f"Midway Airport (Layover) -> {dest_city}",
                "duration": f"{int((duration_hrs/2) * 3600)}s",
                "distanceMeters": int((distance_km/2) * 1000),
                "polyline": {"encodedPolyline": ""},
                "start": None,
                "end": None
            }
        ]
        
    return [{
        "mode": "FLIGHT",
        "duration": f"{int(duration_hrs * 3600)}s",
        "distanceMeters": int(distance_km * 1000),
        "polyline": {"encodedPolyline": ""},
        "start": origin_coords,
        "end": dest_coords
    }]

class NormalizationEngine:
    @staticmethod
    def normalize_route(r, route_name: str, req: RouteRequest):
        duration_str = r.get("duration", "0s")
        original_duration_seconds = int(duration_str.replace("s", ""))
        distance_meters = r.get("distanceMeters", 0)
        mode = r.get("mode", "DRIVE")
        
        duration_seconds = original_duration_seconds
        transit_type = mode.lower()
        
        if mode == "DRIVE" and "legs" in r:
            total_custom_duration = 0
            for leg in r.get("legs", []):
                for step in leg.get("steps", []):
                    step_dist = step.get("distanceMeters", 0)
                    step_dur = int(step.get("duration", "0s").replace("s", ""))
                    step_stat_dur = int(step.get("staticDuration", f"{step_dur}s").replace("s", ""))
                    
                    if step_dur == 0 or step_stat_dur == 0 or step_dist == 0:
                        total_custom_duration += step_dur
                        continue
                        
                    congestion_factor = step_dur / step_stat_dur
                    if congestion_factor < 1.0: congestion_factor = 1.0
                    
                    base_speed_kmh = (step_dist / 1000) / (step_stat_dur / 3600)
                    
                    if base_speed_kmh >= 90: user_speed_kmh = req.expressway_speed
                    elif base_speed_kmh >= 65: user_speed_kmh = req.highway_speed
                    else: user_speed_kmh = req.main_road_speed
                        
                    actual_speed_kmh = user_speed_kmh / congestion_factor
                    actual_speed_mps = actual_speed_kmh * (1000 / 3600)
                    total_custom_duration += step_dist / actual_speed_mps
                    
            if total_custom_duration > 0:
                duration_seconds = total_custom_duration
                
        elif mode == "TRANSIT" and "legs" in r:
            transit_type = "bus" # default
            for leg in r.get("legs", []):
                for step in leg.get("steps", []):
                    transit_details = step.get("transitDetails", {})
                    if transit_details:
                        vehicle_type = transit_details.get("transitLine", {}).get("vehicle", {}).get("type", "")
                        if "RAIL" in vehicle_type or "TRAIN" in vehicle_type or "SUBWAY" in vehicle_type:
                            transit_type = "train"
                            break
                if transit_type == "train": break
                
        duration_mins = max(1, round(duration_seconds / 60))
        distance_km = distance_meters / 1000
        
        if mode == "DRIVE":
            cost_units = distance_km * 0.15
            co2 = distance_km * 0.192
            transit_type = "drive"
        elif mode == "TRANSIT":
            cost_units = distance_km * 0.05
            co2 = distance_km * 0.041 if transit_type == "train" else distance_km * 0.089
        elif mode == "FLIGHT":
            cost_units = distance_km * 0.10 + 50
            co2 = distance_km * 0.25
            transit_type = "flight"
        elif mode == "BICYCLE":
            cost_units = 0
            co2 = 0
            transit_type = "bike"
        else:
            cost_units = distance_km * 0.10
            co2 = distance_km * 0.1
            
        custom_name = r.get("custom_name")
        desc = custom_name if custom_name else f"{transit_type.capitalize()} ({route_name})"
            
        return {
            "mode": mode,
            "type": transit_type,
            "description": desc,
            "duration_mins": duration_mins,
            "cost_units": cost_units,
            "co2_emissions_kg": round(co2, 2),
            "polyline": {"encodedPolyline": r.get("polyline", {}).get("encodedPolyline", "")},
            "start": r.get("start"),
            "end": r.get("end")
        }

@app.post("/api/routes")
async def get_routes(req: RouteRequest):
    if not req.source or not req.destination:
        return {"routes": []}
        
    src = req.source
    dest = req.destination
    stops = req.stops
    
    locations = [src] + stops + [dest]
    
    dest_coords = await get_coordinates(dest)
    weather_data = None
    if dest_coords:
        weather_data = await fetch_weather(dest_coords[0], dest_coords[1])
    
    def inject_layover(duration=90, desc="Transfer / Wait Time"):
        return {
            "mode": "LAYOVER",
            "type": "layover",
            "description": desc,
            "duration_mins": duration,
            "cost_units": 0,
            "co2_emissions_kg": 0,
            "polyline": {"encodedPolyline": ""}
        }
    
    async def orchestrate_flight():
        segments = []
        total_duration = 0
        total_cost = 0
        total_co2 = 0
        for i in range(len(locations)-1):
            leg_src = locations[i]
            leg_dest = locations[i+1]
            
            orig_airport, orig_coords = await fetch_airport_for_location(leg_src)
            dest_airport, dest_airport_coords = await fetch_airport_for_location(leg_dest)
            
            if not orig_coords: orig_coords = await get_coordinates(leg_src)
            if not dest_airport_coords: dest_airport_coords = await get_coordinates(leg_dest)
            
            if not orig_airport or not dest_airport: return None
            
            # Distance check for local flights
            if orig_coords and dest_airport_coords:
                dist_km = haversine(orig_coords, dest_airport_coords)
                if dist_km < 300:
                    return None
            
            tasks = [
                fetch_google_routes(leg_src, orig_airport, "DRIVE"),
                fetch_serpapi_flights(orig_airport, dest_airport, orig_coords, dest_airport_coords),
                fetch_google_routes(dest_airport, leg_dest, "DRIVE")
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            leg_segments = []
            
            for j, res in enumerate(results):
                if not res or isinstance(res, Exception) or len(res) == 0: continue
                
                # Add layover between segments
                if len(leg_segments) > 0:
                    leg_segments.append(inject_layover(120, "Airport Security / Check-in"))
                
                if j == 1 and isinstance(res, list):
                    for idx, flight_leg in enumerate(res):
                        if idx > 0:
                            leg_segments.append(inject_layover(90, "Flight Connection"))
                        name = f"{orig_airport} -> {dest_airport}"
                        norm = NormalizationEngine.normalize_route(flight_leg, name, req)
                        leg_segments.append(norm)
                else:
                    raw = res[0] if isinstance(res, list) else res
                    if j == 0: name = f"{leg_src} -> {orig_airport}"
                    elif j == 2: name = f"{dest_airport} -> {leg_dest}"
                    
                    norm = NormalizationEngine.normalize_route(raw, name, req)
                    leg_segments.append(norm)
            
            if len(segments) > 0 and len(leg_segments) > 0:
                segments.append(inject_layover(120, "Stopover / Rest"))
            
            segments.extend(leg_segments)
                
        if not segments: return None
        for s in segments:
            total_duration += s["duration_mins"]
            total_cost += s["cost_units"]
            total_co2 += s["co2_emissions_kg"]
            
        return {
            "id": "R-FLIGHT", "type": "Flight Orchestrated",
            "total_duration_mins": total_duration,
            "cost_units": total_cost,
            "total_co2_kg": total_co2,
            "segments": segments
        }

    async def orchestrate_transit():
        segments = []
        total_duration = 0
        total_cost = 0
        total_co2 = 0
        for i in range(len(locations)-1):
            leg_src = locations[i]
            leg_dest = locations[i+1]
            res = await fetch_google_routes(leg_src, leg_dest, "TRANSIT", wheelchair=req.wheelchair)
            if not res or len(res) == 0: return None
            
            if i > 0:
                segments.append(inject_layover(60, "Transit Connection"))
                
            norm = NormalizationEngine.normalize_route(res[0], f"{leg_src} -> {leg_dest}", req)
            segments.append(norm)
            
        if not segments: return None
        for s in segments:
            total_duration += s["duration_mins"]
            total_cost += s["cost_units"]
            total_co2 += s["co2_emissions_kg"]
            
        return {
            "id": "R-TRANSIT", "type": "Transit",
            "total_duration_mins": total_duration,
            "cost_units": total_cost,
            "total_co2_kg": total_co2,
            "segments": segments
        }

    # Fire modes concurrently based on allowed_modes
    tasks = []
    
    async def empty_coro(): return None
    
    drive_task = fetch_google_routes(src, dest, "DRIVE", stops) if "DRIVE" in req.allowed_modes else empty_coro()
    bike_task = fetch_google_routes(src, dest, "BICYCLE", stops) if "BICYCLE" in req.allowed_modes else empty_coro()
    transit_task = orchestrate_transit() if "TRANSIT" in req.allowed_modes else empty_coro()
    flight_task = orchestrate_flight() if "FLIGHT" in req.allowed_modes else empty_coro()

    drive_res, bike_res, transit_res, flight_res = await asyncio.gather(
        drive_task, bike_task, transit_task, flight_task, return_exceptions=True
    )
    
    all_options = []
    
    if flight_res and not isinstance(flight_res, Exception):
        all_options.append(flight_res)
        
    if bike_res and not isinstance(bike_res, Exception) and len(bike_res) > 0:
        segments = []
        if len(stops) > 0:
            for i in range(len(locations)-1):
                res = await fetch_google_routes(locations[i], locations[i+1], "BICYCLE")
                if not res or len(res) == 0: continue
                segments.append(NormalizationEngine.normalize_route(res[0], f"{locations[i]} -> {locations[i+1]}", req))
        else:
            segments = [NormalizationEngine.normalize_route(bike_res[0], "Direct Bike", req)]
        
        all_options.append({
            "id": "R-BIKE", "type": "Bicycle",
            "total_duration_mins": sum(s["duration_mins"] for s in segments),
            "cost_units": sum(s.get("cost_units", 0) for s in segments),
            "total_co2_kg": 0,
            "segments": segments
        })
        
    if drive_res and not isinstance(drive_res, Exception) and len(drive_res) > 0:
        # Build drive with layovers for stops
        segments = []
        if len(stops) > 0:
            for i in range(len(locations)-1):
                leg_src = locations[i]
                leg_dest = locations[i+1]
                res = await fetch_google_routes(leg_src, leg_dest, "DRIVE")
                if not res or len(res) == 0: continue
                if i > 0:
                    segments.append(inject_layover(60, "Rest Stop"))
                norm = NormalizationEngine.normalize_route(res[0], f"{leg_src} -> {leg_dest}", req)
                if req.detours and norm.get("start") and norm.get("end"):
                    mid_lat = (norm["start"][0] + norm["end"][0]) / 2
                    mid_lng = (norm["start"][1] + norm["end"][1]) / 2
                    detours = await fetch_detours(mid_lat, mid_lng, req.photos)
                    if detours:
                        norm["detours"] = detours
                segments.append(norm)
        else:
            norm = NormalizationEngine.normalize_route(drive_res[0], f"{src} -> {dest}", req)
            if req.detours and norm.get("start") and norm.get("end"):
                mid_lat = (norm["start"][0] + norm["end"][0]) / 2
                mid_lng = (norm["start"][1] + norm["end"][1]) / 2
                detours = await fetch_detours(mid_lat, mid_lng, req.photos)
                if detours:
                    norm["detours"] = detours
            segments.append(norm)
            
        if segments:
            tot_dur = sum(s["duration_mins"] for s in segments)
            tot_cost = sum(s["cost_units"] for s in segments)
            tot_co2 = sum(s["co2_emissions_kg"] for s in segments)
            all_options.append({
                "id": "R-DRIVE", "type": "Drive",
                "total_duration_mins": tot_dur,
                "cost_units": tot_cost,
                "total_co2_kg": tot_co2,
                "segments": segments
            })
        
    if transit_res and not isinstance(transit_res, Exception):
        all_options.append(transit_res)
        
    if not all_options:
        return {"error": "Could not generate any routes via API."}

    routes = []
    
    # 1. Fastest
    fastest_opt = min(all_options, key=lambda x: x["total_duration_mins"]).copy()
    fastest_opt["type"] = "Fastest"
    routes.append(fastest_opt)
    
    # 2. Cheapest
    if len(all_options) > 1:
        cheapest_opt = min(all_options, key=lambda x: x["cost_units"]).copy()
        cheapest_opt["type"] = "Cheapest"
        routes.append(cheapest_opt)
        
    # 3. Balanced
    if len(all_options) > 2:
        balanced_opt = min(all_options, key=lambda x: x["cost_units"] * x["total_duration_mins"]).copy()
        balanced_opt["type"] = "Balanced"
        routes.append(balanced_opt)
        
    # 4. Eco-Friendly
    if len(all_options) > 0:
        eco_opt = min(all_options, key=lambda x: x["total_co2_kg"]).copy()
        eco_opt["type"] = "Eco-Friendly"
        routes.append(eco_opt)

    packing_list = ["Passport / ID", "Phone & Charger", "Travel Toiletries", "Water Bottle"]
    if weather_data:
        t = weather_data.get("temperature", 20)
        code = weather_data.get("weathercode", 0)
        if t < 10: packing_list.extend(["Heavy Coat", "Warm Gloves", "Beanie / Scarf"])
        elif t > 25: packing_list.extend(["Sunglasses", "Sunscreen", "Light breathable clothes"])
        else: packing_list.extend(["Light Jacket", "Comfortable walking shoes"])
        
        if code in [51,53,55,61,63,65,66,67,80,81,82,95,96,99]:
            packing_list.extend(["Umbrella", "Waterproof Jacket"])

    dest_name = dest.split(',')[0] if dest else "Destination"
    dur_days = req.duration_days
    
    ai_itinerary = []
    if req.planner_mode and req.ai_planner:
        if GEMINI_API_KEY:
            gemini_itin = await generate_gemini_itinerary(dest_name, dur_days, req.interests)
            if gemini_itin:
                ai_itinerary = gemini_itin
                if req.recommend_places:
                    places = await fetch_places_for_itinerary(dest_name, req.interests, req.photos)
                    if places:
                        for d, day_data in enumerate(ai_itinerary):
                            if places: day_data.setdefault("activities", []).append(places.pop(0))
                            if places and d % 2 == 0: day_data.setdefault("activities", []).append(places.pop(0))
        
        if not ai_itinerary:
            if req.recommend_places:
                places = await fetch_places_for_itinerary(dest_name, req.interests, req.photos)
                
                # Pools for variety
                titles_pool = ["Culture & Sightseeing", "Local Experiences", "Hidden Gems & History", "Nature & Relaxation", "City Adventure", "Culinary & Markets", "Arts & Entertainment", "Scenic Views"]
                acts_pool = ["Morning guided city tour", "Explore the vibrant local markets", "Visit a historical landmark", "Relax in a famous park or botanical garden", "Discover the local arts and museum district", "Enjoy a scenic walking tour", "Take a day trip to a nearby attraction", "Experience the local cafe culture"]
                
                for d in range(1, dur_days + 1):
                    day_acts = []
                    if d == 1:
                        day_acts.append("Check-in to accommodation")
                        title = f"Arrival in {dest_name} & Exploration"
                    elif d == dur_days:
                        day_acts.append("Souvenir shopping")
                        day_acts.append("Head to departure point")
                        title = "Relaxation & Departure"
                    else:
                        title = titles_pool[d % len(titles_pool)]
                        day_acts.append(acts_pool[d % len(acts_pool)])
                        
                    # Distribute places
                    if places:
                        for _ in range(2):
                            if places:
                                day_acts.append(places.pop(0))
                                
                    ai_itinerary.append({
                        "day": f"Day {d}",
                        "title": title,
                        "activities": day_acts
                    })
            else:
                for d in range(1, dur_days + 1):
                    if d == 1:
                        ai_itinerary.append({"day": "Day 1", "title": f"Arrival in {dest_name}", "activities": ["Check-in to accommodation", "Explore neighborhood", "Dinner"]})
                    elif d == dur_days and dur_days > 1:
                        ai_itinerary.append({"day": f"Day {dur_days}", "title": "Departure", "activities": ["Morning cafe run", "Head to departure point"]})
                    else:
                        ai_itinerary.append({"day": f"Day {d}", "title": "Sightseeing", "activities": ["Visit main attractions", "Local lunch", "Free time"]})
    
    
    for r in routes:
        total_budget = round(r["cost_units"] * 10, 0) + round(dur_days * 50, 0) + round(dur_days * 120, 0) + round(dur_days * 30, 0)
        r["budget"] = {
            "transport": round(r["cost_units"] * 10, 0), 
            "food": round(dur_days * 50, 0),
            "accommodation": round(dur_days * 120, 0),
            "activities": round(dur_days * 30, 0),
            "total": total_budget
        }
        if req.max_budget and total_budget > req.max_budget:
            r["exceeds_budget"] = True
        else:
            r["exceeds_budget"] = False

    return {"routes": routes, "weather": weather_data, "ai_itinerary": ai_itinerary, "packing_list": packing_list}

@app.get("/")
def read_root():
    with open("index.html", "r") as f:
        return HTMLResponse(content=f.read(), status_code=200)

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    if GROQ_API_KEY:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "openai/gpt-oss-20b",
            "messages": [
                {"role": "system", "content": f"You are WanderSync AI, a helpful travel assistant. Context about their trip: {req.context}"},
                {"role": "user", "content": req.message}
            ]
        }
        async with httpx.AsyncClient() as client:
            try:
                res = await client.post(url, json=payload, headers=headers, timeout=10.0)
                data = res.json()
                if "choices" in data:
                    return {"response": data["choices"][0]["message"]["content"].strip()}
            except Exception:
                pass # Fallback to Gemini on failure

    if not GEMINI_API_KEY:
        return {"response": "I am operating in offline mode. Please add a GEMINI_API_KEY or GROQ_API_KEY to your .env file to enable actual AI."}
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"You are WanderSync AI, a helpful travel assistant. Context about their trip: {req.context}\n\nUser: {req.message}\nAssistant:"
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7}
    }
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload, timeout=10.0)
            data = res.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return {"response": text.strip()}
        except Exception as e:
            return {"response": "Sorry, I'm having trouble connecting to my AI brain right now."}

async def generate_gemini_itinerary(dest_name, dur_days, interests):
    cache_key = get_cache_key("gemini_itinerary", dest_name, dur_days, interests)
    if cache_key in ITINERARY_CACHE:
        return ITINERARY_CACHE[cache_key]
        
    if not GEMINI_API_KEY:
        return None
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"Create a {dur_days}-day travel itinerary for {dest_name}. The user is interested in: {interests}. Return strictly in JSON format as a list of objects, each with 'day' (e.g. 'Day 1'), 'title' (a short day title), and 'activities' (a list of 2-3 strings describing activities). Do not use markdown formatting like ```json, just return the raw JSON array."
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7}
    }
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload, timeout=15.0)
            data = res.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            text = text.strip()
            if text.startswith("```json"):
                text = text.split("```json")[1].split("```")[0].strip()
            elif text.startswith("```"):
                text = text.split("```")[1].split("```")[0].strip()
            parsed = json.loads(text)
            ITINERARY_CACHE[cache_key] = parsed
            return parsed
        except Exception:
            return None



@app.websocket("/ws/{trip_id}")
async def websocket_endpoint(websocket: WebSocket, trip_id: str):
    await manager.connect(websocket, trip_id)
    try:
        while True:
            data = await websocket.receive_text()
            # Broadcast the state change to everyone else in the room
            await manager.broadcast(data, trip_id)
    except WebSocketDisconnect:
        manager.disconnect(websocket, trip_id)

class UserAuth(BaseModel):
    username: str
    password: str

@app.post("/api/signup")
def signup(auth: UserAuth, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == auth.username).first()
    if user:
        raise HTTPException(status_code=400, detail="Username already registered")
    
    # In a real app, hash this password!
    new_user = models.User(username=auth.username, hashed_password=auth.password)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": "User created successfully"}

@app.post("/api/login")
def login(auth: UserAuth, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == auth.username).first()
    if not user or user.hashed_password != auth.password:
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    # In a real app, return a JWT token!
    return {"token": str(user.id), "username": user.username}

class TripSave(BaseModel):
    user_id: int
    trip_id: str
    destination: str
    data_json: str

@app.post("/api/trips/save")
def save_trip(trip: TripSave, db: Session = Depends(get_db)):
    db_trip = db.query(models.Trip).filter(models.Trip.trip_id == trip.trip_id).first()
    if db_trip:
        db_trip.data_json = trip.data_json
        db_trip.destination = trip.destination
    else:
        new_trip = models.Trip(**trip.dict())
        db.add(new_trip)
    db.commit()
    return {"message": "Trip saved successfully"}

@app.get("/api/trips/{trip_id}")
def load_trip(trip_id: str, db: Session = Depends(get_db)):
    db_trip = db.query(models.Trip).filter(models.Trip.trip_id == trip_id).first()
    if not db_trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    return {"data_json": db_trip.data_json}

app.mount("/", StaticFiles(directory="."), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
