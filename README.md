# 🌍 WanderSync (travel-anywhere)

**WanderSync** is a modern, AI-powered travel aggregator and progressive web app (PWA) that calculates multi-modal travel routes, generates dynamic itineraries, and allows collaborative planning in real-time.

## ✨ Features
- **Multi-Modal Routing:** Seamlessly orchestrates Drive, Flight, Transit, and Bicycle legs using Google Routes API and SerpAPI (Google Flights).
- **AI Itinerary Generation:** Leverages Google's Gemini 3.6 Pro model and Groq Open-Source API to generate day-by-day itineraries and personalized packing lists.
- **Collaborative Multiplayer:** Uses WebSockets to sync the map, routes, and chat across multiple users in real-time. Just share the `?trip=id` link!
- **Progressive Web App (PWA):** Installable on Desktop/Mobile with offline caching support via Service Workers.
- **Integrated AI Chatbot:** Interactive Markdown-supported Chatbot to help you plan your trip natively in the UI.
- **User Authentication:** Built-in SQLite + SQLAlchemy local database to sign up and save your favorite trips.

## 🚀 Tech Stack
- **Backend:** Python, FastAPI, WebSockets, Uvicorn, SQLAlchemy
- **Frontend:** Vanilla JS, HTML/CSS, Google Maps API, `marked.js`
- **APIs:** Google Routes API, SerpAPI, Google Gemini, Groq (Llama/GPT)

## 🛠️ Local Setup
1. Clone the repository.
2. Install dependencies: `pip install fastapi uvicorn httpx sqlalchemy googlemaps haversine`
3. Create a `.env` file with your API keys:
   ```env
   GOOGLE_MAPS_API_KEY=your_key
   GOOGLE_ROUTES_API_KEY=your_key
   SERPAPI_API_KEY=your_key
   GEMINI_API_KEY=your_key
   GROQ_API_KEY=your_key
   ```
4. Run the server: `uvicorn main:app --port 8000 --reload`
5. Open `http://127.0.0.1:8000` in your browser.
