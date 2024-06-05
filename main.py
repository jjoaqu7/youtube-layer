import os
import re
import json
import asyncio
import aiohttp
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI

#region
#After activating your virtual environment, set your openai key using `$env:OPENAI_API_KEY = 'your_openai_api_key_here'` in the powershel python terminal
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
youtube_api_key = "AIzaSyDyB1vY_cVgdhYAbnmhxukl2jLcOa4Wu84"
#endregion

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
    print(f"Main Topic: {topic}")
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

# Main async function
async def main():
    query = input("Enter your search query: ")
    main_topic = get_topic_from_query(query)

    request = youtube.search().list(
        part='snippet,id',
        q=main_topic,
        type='video',
        maxResults=3
    )
    response = request.execute()

    if 'items' in response and response['items']:
        tasks = []
        async with aiohttp.ClientSession() as session:
            for item in response['items']:
                video_id = item['id']['videoId']
                video_title = item['snippet']['title']
                sanitized_title = sanitize_filename(video_title)
                video_url = f'https://www.youtube.com/watch?v={video_id}'
                print(f"\nVideo URL: {video_url}")

                try:
                    transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['en'])
                    transcript_text = "\n".join([f"{t['start']}s - {t['start'] + t['duration']}s: {t['text']}" for t in transcript_list])

                    # Save the transcript to a file
                    with open(f"transcripts/{sanitized_title}.txt", "w", encoding='utf-8') as file:
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

            # process and print the results
            for analysis, item in zip(analyses, response['items']):
                video_id = item['id']['videoId']
                if analysis:
                    try:
                        analysis_data = analysis

                        # print the analysis with hyperlinks
                        print("----------Analysis with links:----------")
                        for info in analysis_data["relevant_information"]:
                            timestamp_link = create_timestamp_link(video_id, info["start_time"])
                            print(f"Title: {info['title']}")
                            print(f"Description: {info['description']}")
                            print(f"Text: {info['text']}")
                            print(f"Timestamp: {timestamp_link}")
                            print("-----")

                    except Exception as e:
                        print(f"Error analyzing transcript: {e}")
    else:
        print("No videos found for the query.")

if __name__ == "__main__":
    asyncio.run(main())
