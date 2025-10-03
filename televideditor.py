import os
import telebot
from telebot import types
import time
import requests
from PIL import Image, ImageDraw, ImageFont
import textwrap
import subprocess
import json
import logging
import base64
import re
import multiprocessing
import threading # <-- Using threading for lightweight background tasks
from threading import Lock

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# --- Constants and Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WORKER_PUBLIC_URL = os.environ.get("WORKER_PUBLIC_URL")
RAILWAY_API_TOKEN = os.environ.get("RAILWAY_API_TOKEN") # <-- Add this
RAILWAY_SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID") # <-- Add this

if not all([BOT_TOKEN, WORKER_PUBLIC_URL, RAILWAY_API_TOKEN, RAILWAY_SERVICE_ID]):
    raise ValueError("BOT_TOKEN, WORKER_PUBLIC_URL, RAILWAY_API_TOKEN, and RAILWAY_SERVICE_ID environment variables must be set!")

# --- Video Processing Constants ---
COMP_WIDTH = 1080
COMP_HEIGHT = 1920
COMP_SIZE_STR = f"{COMP_WIDTH}x{COMP_HEIGHT}"
BACKGROUND_COLOR = "black"
FPS = 30
IMAGE_DURATION = 8
FADE_IN_DURATION = 6
MEDIA_Y_OFFSET = 100
CAPTION_V_PADDING = 37
CAPTION_FONT_SIZE = 55
CAPTION_TOP_PADDING_LINES = 0
CAPTION_LINE_SPACING = 12
CAPTION_FONT = "Montserrat-ExtraBold"
CAPTION_TEXT_COLOR = (0, 0, 0)
CAPTION_BG_COLOR = (255, 255, 255)

# --- File Paths and Session Management ---
DOWNLOAD_PATH = "downloads"
OUTPUT_PATH = "outputs"
SESSION_TIMEOUT = 1800  # 30 minutes in seconds

# --- Bot Initialization and Thread-Safe Session Management ---
bot = telebot.TeleBot(BOT_TOKEN)
user_data = {}
user_data_lock = Lock()

# --- Helper Functions ---

def cleanup_files(file_list):
    """Safely delete a list of files."""
    for file_path in file_list:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logging.error(f"Error deleting file {file_path}: {e}")

def create_directories():
    """Create necessary directories if they don't exist."""
    for path in [DOWNLOAD_PATH, OUTPUT_PATH]:
        if not os.path.exists(path):
            os.makedirs(path)
            
# --- stop itself ---

def stop_railway_deployment():
    """
    Notifies the worker to start listening again and then stops the Railway deployment.
    """
    # --- MODIFIED PART: Notify the worker to reset its state before stopping ---
    reset_url = f"{WORKER_PUBLIC_URL}/reset"
    logging.info(f"Sending GET request to worker reset endpoint: {reset_url}")
    try:
        # Send a GET request with a short timeout.
        # We don't need to process the response, just ensure it's sent.
        response = requests.get(reset_url, timeout=10)
        if response.status_code == 200:
            logging.info("Successfully notified worker to reset.")
        else:
            logging.warning(f"Worker reset endpoint returned status {response.status_code}: {response.text}")
    except requests.exceptions.RequestException as e:
        # Log the error but continue, as stopping the deployment is more critical.
        logging.error(f"Failed to send reset signal to worker: {e}")
    # --- END OF MODIFICATION ---

    logging.info("Attempting to stop Railway deployment...")
    api_token = os.environ.get("RAILWAY_API_TOKEN")
    service_id = os.environ.get("RAILWAY_SERVICE_ID")

    if not api_token or not service_id:
        logging.warning("RAILWAY_API_TOKEN or RAILWAY_SERVICE_ID is not set. Skipping stop.")
        return

    graphql_url = "https://backboard.railway.app/graphql/v2"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }

    # Step 1: Get the latest deployment ID
    get_id_query = {
        "query": """
            query getLatestDeployment($serviceId: String!) {
                service(id: $serviceId) {
                    deployments(first: 1) {
                        edges {
                            node { id }
                        }
                    }
                }
            }
        """,
        "variables": {"serviceId": service_id}
    }

    try:
        response = requests.post(graphql_url, json=get_id_query, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        deployment_id = data['data']['service']['deployments']['edges'][0]['node']['id']
        logging.info(f"Successfully fetched latest deployment ID: {deployment_id}")

    except (requests.exceptions.RequestException, KeyError, IndexError) as e:
        logging.error(f"Failed to get Railway deployment ID: {e}")
        logging.error(f"Response from Railway: {response.text if 'response' in locals() else 'No response'}")
        return # Stop if we can't get the ID

    # Step 2: Trigger the stop using the deployment ID
    stop_mutation = {
        "query": """
            mutation deploymentStop($id: String!) {
                deploymentStop(id: $id)
            }
        """,
        "variables": {"id": deployment_id}
    }

    try:
        response = requests.post(graphql_url, json=stop_mutation, headers=headers, timeout=15)
        response.raise_for_status()
        logging.info("Successfully sent stop command to Railway.")

    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to send stop command to Railway: {e}")
        logging.error(f"Response from Railway: {response.text if 'response' in locals() else 'No response'}")
        

def get_media_dimensions(media_path, media_type):
    """Get dimensions and duration of media using ffprobe or PIL."""
    if media_type == 'image':
        with Image.open(media_path) as img:
            return img.width, img.height, IMAGE_DURATION
    else:
        command = [
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,duration',
            '-of', 'json', media_path
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
            data = json.loads(result.stdout)['streams'][0]
            return data['width'], data['height'], float(data['duration'])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError) as e:
            logging.error(f"FFprobe failed: {e}")
            return None, None, None

def create_caption_image(text, chat_id):
    """Create a transparent PNG image for the caption text."""
    padded_text = ("\n" * CAPTION_TOP_PADDING_LINES) + text
    font_path = f"{CAPTION_FONT}.ttf"
    font = ImageFont.truetype(font_path, CAPTION_FONT_SIZE)
    
    final_lines = [item for line in padded_text.split('\n') for item in textwrap.wrap(line, width=30, break_long_words=True) or ['']]
    wrapped_text = "\n".join(final_lines)

    # Use a dummy draw object to calculate text size
    dummy_draw = ImageDraw.Draw(Image.new('RGB', (0,0)))
    text_bbox = dummy_draw.multiline_textbbox((0, 0), wrapped_text, font=font, align="center", spacing=CAPTION_LINE_SPACING)
    
    text_height = text_bbox[3] - text_bbox[1]
    rect_height = text_height + (2 * CAPTION_V_PADDING)
    img_height = int(rect_height)

    img = Image.new('RGBA', (COMP_WIDTH, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    draw.rectangle([(0, 0), (COMP_WIDTH, img_height)], fill=CAPTION_BG_COLOR)
    draw.multiline_text(
        (COMP_WIDTH / 2, img_height / 2),
        wrapped_text,
        font=font,
        fill=CAPTION_TEXT_COLOR,
        anchor="mm",
        align="center",
        spacing=CAPTION_LINE_SPACING
    )

    caption_image_path = os.path.join(OUTPUT_PATH, f"caption_{chat_id}.png")
    img.save(caption_image_path)
    return caption_image_path, rect_height

def extract_frame_from_video(video_path, duration, chat_id):
    """Extract a frame from the midpoint of a video."""
    frame_path = os.path.join(OUTPUT_PATH, f"frame_{chat_id}.jpg")
    midpoint = duration / 2
    command = [
        'ffmpeg', '-y', '-i', video_path, '-ss', str(midpoint),
        '-vframes', '1', frame_path
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        return frame_path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logging.error(f"FFmpeg frame extraction failed: {getattr(e, 'stderr', e)}")
        return None

# --- Background Cleanup Thread ---

def cleanup_stale_sessions():
    """Periodically cleans up old, incomplete user sessions."""
    while True:
        time.sleep(300) # Check every 5 minutes
        stale_users = []
        with user_data_lock:
            now = time.time()
            for chat_id, data in user_data.items():
                if now - data.get('timestamp', now) > SESSION_TIMEOUT:
                    stale_users.append(chat_id)
            
            for chat_id in stale_users:
                logging.info(f"Cleaning up stale session for chat_id: {chat_id}")
                session_data = user_data.pop(chat_id, {})
                cleanup_files([session_data.get('media_path')])

# --- Telegram Handlers ---

@bot.message_handler(content_types=['photo', 'video'])
def handle_media(message):
    chat_id = message.chat.id
    with user_data_lock:
        if chat_id in user_data and user_data[chat_id].get('state') in ['downloading', 'processing']:
            bot.reply_to(message, "I'm currently busy with your previous request. Please wait.")
            return

        user_data[chat_id] = {'state': 'downloading', 'timestamp': time.time()}

    download_message = bot.reply_to(message, "⬇️ Media detected. Downloading...")

    try:
        if message.content_type == 'photo':
            file_id = message.photo[-1].file_id
            media_type = 'image'
        else: # video
            file_id = message.video.file_id
            media_type = 'video'
        
        file_info = bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        file_extension = os.path.splitext(file_info.file_path)[1]
        save_path = os.path.join(DOWNLOAD_PATH, f"{chat_id}_{file_id}{file_extension}")

        # Stream the download to use less memory
        with requests.get(file_url, stream=True) as r:
            r.raise_for_status()
            with open(save_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        with user_data_lock:
            user_data[chat_id].update({
                'media_path': save_path,
                'media_type': media_type,
                'state': 'awaiting_caption',
                'timestamp': time.time()
            })
        
        bot.edit_message_text("✅ Media received! Now, please send the caption text.", chat_id, download_message.message_id)

    except Exception as e:
        logging.error(f"Error in handle_media for chat {chat_id}: {e}", exc_info=True)
        bot.edit_message_text("❌ An error occurred while downloading your media.", chat_id, download_message.message_id)
        with user_data_lock:
            if chat_id in user_data:
                del user_data[chat_id]

@bot.message_handler(func=lambda message: user_data.get(message.chat.id, {}).get('state') == 'awaiting_caption')
def handle_text(message):
    chat_id = message.chat.id
    
    with user_data_lock:
        if chat_id not in user_data: return
        user_data[chat_id]['caption_text'] = message.text
        user_data[chat_id]['state'] = 'awaiting_fade_choice'
        user_data[chat_id]['timestamp'] = time.time()

    markup = types.InlineKeyboardMarkup(row_width=2)
    yes_button = types.InlineKeyboardButton("Yes", callback_data="fade_yes")
    no_button = types.InlineKeyboardButton("No", callback_data="fade_no")
    markup.add(yes_button, no_button)
    
    bot.send_message(chat_id, "Apply a fade-in effect?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('fade_'))
def handle_fade_choice(call):
    chat_id = call.message.chat.id
    
    with user_data_lock:
        session = user_data.get(chat_id)
        if not session or session.get('state') != 'awaiting_fade_choice':
            bot.answer_callback_query(call.id, "This action has expired.", show_alert=True)
            return
        
        # Prevent double-clicks and further processing
        session['state'] = 'processing'
        # Immediately remove from user_data to prevent another process from starting
        session_to_process = user_data.pop(chat_id)

    bot.answer_callback_query(call.id)
    apply_fade = call.data == "fade_yes"
    choice_text = "Yes" if apply_fade else "No"
    bot.edit_message_text(f"Fade-in effect: {choice_text}", chat_id, call.message.message_id)
    
    processing_message = bot.send_message(chat_id, "⚙️ Your video is being created...")
    
    # Use multiprocessing for the CPU-bound ffmpeg task
    p = multiprocessing.Process(
        target=isolated_video_processing_task,
        args=(
            chat_id,
            session_to_process['media_path'],
            session_to_process['media_type'],
            session_to_process['caption_text'],
            apply_fade,
            processing_message.message_id
        ),
        daemon=True
    )
    p.start()

@bot.message_handler(func=lambda message: True)
def handle_other_messages(message):
    bot.reply_to(message, "Please start by sending an image or a video.")

# --- Isolated Processing and Upload Functions ---

def upload_and_process(chat_id, video_path, frame_path):
    """Uploads video and a frame to the worker for further processing."""
    worker_url = f"{WORKER_PUBLIC_URL}/process"
    try:
        with open(frame_path, "rb") as image_file, open(video_path, 'rb') as video_file:
            image_data = base64.b64encode(image_file.read()).decode('utf-8')
            files = {
                'video': ('final_video.mp4', video_file, 'video/mp4'),
                'image_data': (None, image_data),
                'chat_id': (None, str(chat_id))
            }
            response = requests.post(worker_url, files=files, timeout=60)
            response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Error uploading to worker for chat {chat_id}: {e}")
        return False

def isolated_video_processing_task(chat_id, media_path, media_type, caption_text, apply_fade, message_id):
    """A self-contained function to run in a separate process."""
    
    def send_bot_message(text, parse_mode=None):
        """Helper to send status updates from the isolated process."""
        payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
        if parse_mode: payload['parse_mode'] = parse_mode
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json=payload, timeout=10)
        except requests.exceptions.RequestException as e:
            logging.error(f"Isolated process for chat {chat_id} failed to send message: {e}")

    output_filepath = os.path.join(OUTPUT_PATH, f"output_{chat_id}.mp4")
    caption_image_path, frame_path = None, None
    files_to_clean = [media_path]

    try:
        media_w, media_h, final_duration = get_media_dimensions(media_path, media_type)
        if not all([media_w, media_h, final_duration]):
            raise ValueError("Could not get media dimensions.")

        caption_image_path, caption_height = create_caption_image(caption_text, chat_id)
        files_to_clean.append(caption_image_path)
        
        scale_ratio = COMP_WIDTH / media_w
        scaled_media_h = int(media_h * scale_ratio)
        media_y_pos = (COMP_HEIGHT / 2 - scaled_media_h / 2) + MEDIA_Y_OFFSET
        caption_y_pos = media_y_pos - caption_height + 1

        # Base ffmpeg command
        command = [
            'ffmpeg', '-y',
            '-f', 'lavfi', '-i', f'color=c={BACKGROUND_COLOR}:s={COMP_SIZE_STR}:d={final_duration}'
        ]
        if media_type == 'image':
            command.extend(['-loop', '1', '-t', str(final_duration)])
        command.extend(['-i', media_path, '-i', caption_image_path])

        # --- EFFICIENT FILTER_COMPLEX ---
        # This version pipes filters directly, avoiding large intermediate memory buffers.
        filter_parts = [
            f"[1:v]scale={COMP_WIDTH}:-1,setpts=PTS-STARTPTS[scaled_media]",
        ]

        media_layer = "[scaled_media]"
        if apply_fade:
            filter_parts.extend([
                f"color=c=black:s={COMP_WIDTH}x{scaled_media_h+1}:d={final_duration},format=rgba,fade=t=out:st=0:d={FADE_IN_DURATION}[fade_layer]",
                f"[scaled_media][fade_layer]overlay=0:0[media_with_fade]"
            ])
            media_layer = "[media_with_fade]"

        filter_parts.extend([
            f"[0:v]{media_layer}overlay=(W-w)/2:{media_y_pos}[bg_with_media]",
            f"[bg_with_media][2:v]overlay=(W-w)/2:{caption_y_pos}[final_v]"
        ])

        filter_complex = ";".join(filter_parts)
        
        map_args = ['-map', '[final_v]']
        if media_type == 'video':
            filter_complex += ";[1:a]asetpts=PTS-STARTPTS[final_a]"
            map_args.extend(['-map', '[final_a]'])
        
        command.extend([
            '-filter_complex', filter_complex, *map_args,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-threads', str(multiprocessing.cpu_count()), # Use available cores
            '-c:a', 'aac', '-b:a', '192k',
            '-r', str(FPS), '-pix_fmt', 'yuv420p',
            output_filepath
        ])
        
        # Execute ffmpeg
        result = subprocess.run(command, capture_output=True, text=True, timeout=300) # 5-minute timeout
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, command, stderr=result.stderr)
        
        logging.info(f"FFmpeg processing finished for chat {chat_id}.")
        files_to_clean.append(output_filepath)

        send_bot_message("⬆️ Preparing to upload...")
        frame_path = extract_frame_from_video(output_filepath, final_duration, chat_id)
        if frame_path:
            files_to_clean.append(frame_path)
            if upload_and_process(chat_id, output_filepath, frame_path):
                send_bot_message("✅ Success! Your video will be sent shortly.")
            else:
                send_bot_message("⚠️ Upload to our servers failed. Please try again.")
        else:
            send_bot_message("⚠️ Could not prepare the video for final processing.")

    except subprocess.CalledProcessError as e:
        error_snippet = (e.stderr or "No stderr output.")[-1000:]
        logging.error(f"FFmpeg failed for chat {chat_id}:\n{error_snippet}")
        send_bot_message(f"An error occurred during video creation:\n`{error_snippet}`", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error in isolated process for chat {chat_id}: {e}", exc_info=True)
        send_bot_message("An unexpected server error occurred. Please try again.")
    finally:
        cleanup_files(files_to_clean)
        stop_railway_deployment() # <--- NOTIFIES WORKER AND THEN STOPS BOT

# --- Main Bot Loop ---
if __name__ == '__main__':
    logging.info("Starting bot...")
    create_directories()
    
    # Start the background thread for cleaning up stale sessions
    cleanup_thread = threading.Thread(target=cleanup_stale_sessions, daemon=True)
    cleanup_thread.start()
    
    while True:
        try:
            logging.info("Bot is polling for messages...")
            bot.polling(none_stop=True)
        except Exception as e:
            logging.error(f"Bot polling loop crashed: {e}", exc_info=True)
            time.sleep(15)
