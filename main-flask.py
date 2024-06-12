import os
import re
import json
import asyncio
import aiohttp
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI
from flask import Flask, request, jsonify
import time
from google.cloud import secretmanager
from google.auth import default

app = Flask(__name__)

# Authenticate using default credentials
credentials, project_id = default()
if credentials is not None:
    print(f"Authenticated account: {credentials.service_account_email}")
else:
    print("No credentials found.")
    raise ValueError("Credentials not found")

def access_secret_version(project_id, secret_id, version_id):
    client = secretmanager.SecretManagerServiceClient(credentials=credentials)
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"
    print(f"NAME>> {name}")
    response = client.access_secret_version(name=name)
    payload = response.payload.data.decode("UTF-8")
    return payload

# Fetch the OpenAI API key from Secret Manager
secret_id = os.getenv("SECRET_ID")
version_id = os.getenv("SECRET_VERSION")
if secret_id is None or version_id is None:
    print("No Secret ID or Version ID set")
    raise ValueError("SECRET_ID or SECRET_VERSION environment variables not set")
    
openai_api_key = access_secret_version(project_id, secret_id, version_id)

if openai_api_key:
    print(f"OpenAI API Key successfully retrieved: {openai_api_key[:4]} ...") 
else:
    print("OpenAI API Key not found. Please check the environment variable.")
    raise ValueError("OpenAI API Key not found")

client = OpenAI(api_key=openai_api_key)

# Fetch the YouTube API key from Secret Manager
secret_youtube_id = os.getenv("SECRET_YOUTUBE_ID")
if secret_youtube_id is None:
    raise ValueError("SECRET_YOUTUBE_ID environment variable not set")
youtube_api_key = access_secret_version(project_id, secret_youtube_id, version_id)

if youtube_api_key:
    print(f"YouTube API Key successfully retrieved: {youtube_api_key[:4]} ...") 
else:
    print("YouTube API Key not found. Please check the environment variable.")
    raise ValueError("YouTube API Key not found")
youtube = build('youtube', 'v3', developerKey=youtube_api_key)

def sanitize_filename(filename):
    return re.sub(r'[\/:*?"<>|]', '_', filename)

def get_topic_from_query(query):
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": f"""
                Your tasks are the following:
                0. Read the user's query: {query} 
                1. Then you will identify and extract the principal main topic of the user's query (NOTE: THERE CAN ONLY BE ONE PRINCIPAL/MAIN )
                2. Check if the user's query is inappropriate or nonsense input.
                3. You must return the main topic only without any other text, just the sole main topic, as standalone text.
                4. DO NOT include anything else other than the main topic, NO SYMBOLS, NO DECORATORS, NO CURLEY BRACKETS, JUST THE MAIN TOPIC!
            """}
        ],
        temperature=0.5,
        max_tokens=150
    )
    topic = response.choices[0].message.content.strip()
    return topic

def create_timestamp_link(video_id, start):
    return f"https://www.youtube.com/watch?v={video_id}&t={int(start)}s"

def analyze_transcript_sync(transcript_text, query):
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": f"""Analyze the following transcript and extract the most relevant information related to the query '{query}'. 
            Provide timestamps where this information can be found in JSON format. The JSON schema should include 2 instances of relevant information and the following structure: {{
                "relevant_information": [
                    {{
                        "title": "string",
                        "description": "string",
                        "text": "string",
                        "start_time": "number",
                        "end_time": "number",
                    }}
                ]
            }}: {transcript_text}"""}
        ],
        temperature=0.5,
        max_tokens=500,
    )

    content = response.choices[0].message.content.strip()
    content = content.strip('```json').strip('```')  
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        print(f"Error decoding JSON response: {content}")
        return None

async def fetch_transcripts_and_analyze(videos, query):
    tasks = []
    async with aiohttp.ClientSession() as session:
        for item in videos:
            video_id = item['id']['videoId']
            video_title = item['snippet']['title']
            sanitized_title = sanitize_filename(video_title)
            video_url = f'https://www.youtube.com/watch?v={video_id}'

            try:
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['en'])
                transcript_text = "\n".join([f"{t['start']}s - {t['start'] + t['duration']}s: {t['text']}" for t in transcript_list])

                # Save the transcript to a file
                os.makedirs("/tmp/transcripts", exist_ok=True)
                with open(f"/tmp/transcripts/{sanitized_title}.txt", "w", encoding='utf-8') as file:
                    file.write(f"Video URL: {video_url}\n\n")
                    file.write(transcript_text)


                tasks.append(asyncio.to_thread(analyze_transcript_sync, transcript_text, query))

            except Exception as e:
                if "Could not retrieve a transcript" in str(e):
                    print(f"No English transcript available for video: {video_url}")
                else:
                    print(f"Error fetching transcript: {e}")

        # Execute all tasks concurrently
        analyses = await asyncio.gather(*tasks)
        return analyses

@app.route('/search', methods=['POST'])
def search_videos():
    data = request.json
    query = data.get("query")
    start_time = time.time()  # Record the start time

    if not query:
        return jsonify({"error": "Query is required"}), 400

    main_topic = get_topic_from_query(query)

    request_youtube = youtube.search().list(
        part='snippet,id',
        q=main_topic,
        type='video',
        maxResults=3
    )
    response = request_youtube.execute()

    if 'items' in response and response['items']:
        videos = response['items']
        analyses = asyncio.run(fetch_transcripts_and_analyze(videos, query))

        results = []
        for analysis, item in zip(analyses, videos):
            video_id = item['id']['videoId']
            if analysis:
                analysis_data = analysis
                for info in analysis_data["relevant_information"]:
                    timestamp_link = create_timestamp_link(video_id, info["start_time"])
                    results.append({
                        "video_url": f"https://www.youtube.com/watch?v={video_id}",
                        "title": info['title'],
                        "description": info['description'],
                        "text": info['text'],
                        "timestamp": timestamp_link
                    })
        end_time = time.time()  # Record the end time
        print(f"Time taken: {end_time - start_time} seconds")  # Print the duration   
        return jsonify(results)
    else:
        return jsonify({"message": "No videos found for the query."})

if __name__ == "__main__":
    app.run(debug=True)