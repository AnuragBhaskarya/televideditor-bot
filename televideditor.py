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
import threading # <-- MODIFICATION: Added for background cleanup

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# --- Constants and Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WORKER_PUBLIC_URL = os.environ.get("WORKER_PUBLIC_URL")

if not BOT_TOKEN or not WORKER_PUBLIC_URL:
    raise ValueError("BOT_TOKEN and WORKER_PUBLIC_URL environment variables must be set!")

# --- Efficiency Constants ---
SESSION_TIMEOUT_SECONDS = 1800  # 30 minutes
CLEANUP_INTERVAL_SECONDS = 300 # 5 minutes

# --- Video Generation Constants ---
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
DOWNLOAD_PATH = "downloads"
OUTPUT_PATH = "outputs"

# --- Bot Initialization ---
bot = telebot.TeleBot(BOT_TOKEN)
manager = multiprocessing.Manager()
user_data = manager.dict()


# --- Helper Functions ---

def cleanup_files(file_list):
    """Safely removes a list of files if they exist."""
    for file_path in file_list:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logging.error(f"Error deleting file {file_path}: {e}")

def create_directories():
    """Creates necessary directories on startup."""
    if not os.path.exists(DOWNLOAD_PATH): os.makedirs(DOWNLOAD_PATH)
    if not os.path.exists(OUTPUT_PATH): os.makedirs(OUTPUT_PATH)

def get_media_dimensions(media_path, media_type):
    if media_type == 'image':
        with Image.open(media_path) as img: return img.width, img.height
    else:
        command = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,duration', '-of', 'json', media_path]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        stream_data = data['streams'][0]
        return stream_data['width'], stream_data['height'], float(stream_data['duration'])

def create_caption_image(text, media_width, chat_id):
    padded_text = ("\n" * CAPTION_TOP_PADDING_LINES) + text
    font_path = f"{CAPTION_FONT}.ttf"
    font = ImageFont.truetype(font_path, CAPTION_FONT_SIZE)
    
    final_lines = [item for sublist in [textwrap.wrap(line, width=30, break_long_words=True) or [''] for line in padded_text.split('\n')] for item in sublist]
    wrapped_text = "\n".join(final_lines)

    # Dummy draw to calculate text size accurately
    dummy_draw = ImageDraw.Draw(Image.new('RGB', (0,0)))
    text_bbox = dummy_draw.multiline_textbbox((0, 0), wrapped_text, font=font, align="center", spacing=CAPTION_LINE_SPACING)
    
    text_height = text_bbox[3] - text_bbox[1]
    img_height = text_height + (2 * CAPTION_V_PADDING)
    
    img = Image.new('RGBA', (COMP_WIDTH, int(img_height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    draw.rectangle([(0, 0), (COMP_WIDTH, img_height)], fill=CAPTION_BG_COLOR)
    draw.multiline_text(
        (COMP_WIDTH / 2, img_height / 2),
        wrapped_text, font=font, fill=CAPTION_TEXT_COLOR,
        anchor="mm", align="center", spacing=CAPTION_LINE_SPACING
    )
    
    caption_image_path = os.path.join(OUTPUT_PATH, f"caption_{chat_id}.png")
    img.save(caption_image_path)
    return caption_image_path, img_height

def extract_frame_from_video(video_path, duration, chat_id):
    frame_path = os.path.join(OUTPUT_PATH, f"frame_{chat_id}.jpg")
    midpoint = duration / 2
    command = ['ffmpeg', '-y', '-i', video_path, '-ss', str(midpoint), '-vframes', '1', frame_path]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        logging.info(f"Successfully extracted frame to {frame_path}")
        return frame_path
    except subprocess.CalledProcessError as e:
        logging.error(f"FFmpeg frame extraction failed: {e.stderr}")
        return None

# --- MODIFICATION: Background task to clean up stale user sessions ---
def cleanup_stale_sessions():
    """Periodically checks for and removes old, incomplete user sessions."""
    while True:
        try:
            stale_users = []
            now = time.time()
            # Safely iterate over a copy of the keys to avoid runtime errors
            for chat_id in list(user_data.keys()):
                session = user_data.get(chat_id)
                if session and (now - session.get('timestamp', now) > SESSION_TIMEOUT_SECONDS):
                    stale_users.append(chat_id)
            
            for chat_id in stale_users:
                logging.info(f"Cleaning up stale session for chat_id: {chat_id}")
                session_data = user_data.pop(chat_id, None)
                if session_data:
                    cleanup_files([session_data.get('media_path')])
        except Exception as e:
            logging.error(f"Error in cleanup thread: {e}", exc_info=True)
        
        time.sleep(CLEANUP_INTERVAL_SECONDS)

# --- Telegram Handlers ---

@bot.message_handler(content_types=['photo', 'video'])
def handle_media(message):
    chat_id = message.chat.id
    if user_data.get(chat_id, {}).get('state') in ['downloading', 'processing']:
        bot.reply_to(message, "I'm currently busy. Please wait until the current process is finished.")
        return

    user_data[chat_id] = {'state': 'downloading', 'timestamp': time.time()}
    download_message = bot.reply_to(message, "⬇️ Media detected. Starting download...")

    try:
        file_id = message.photo[-1].file_id if message.content_type == 'photo' else message.video.file_id
        media_type = 'image' if message.content_type == 'photo' else 'video'
        
        file_info = bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        file_extension = os.path.splitext(file_info.file_path)[1]
        save_path = os.path.join(DOWNLOAD_PATH, f"{chat_id}_{file_id}{file_extension}")

        # Efficiently download file in chunks without loading into memory
        with requests.get(file_url, stream=True) as r:
            r.raise_for_status()
            with open(save_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        # Update user data with new state and info
        user_data[chat_id] = {
            'media_path': save_path,
            'media_type': media_type,
            'state': 'awaiting_caption',
            'timestamp': time.time() # <-- MODIFICATION: Update timestamp
        }
        bot.edit_message_text("✅ Media received! Now, please send the top caption text.", chat_id, download_message.message_id)

    except Exception as e:
        logging.error(f"Error in handle_media: {e}", exc_info=True)
        bot.edit_message_text(f"❌ An error occurred while processing your media.", chat_id, download_message.message_id)
        if chat_id in user_data: del user_data[chat_id]

@bot.message_handler(func=lambda message: user_data.get(message.chat.id, {}).get('state') == 'awaiting_caption')
def handle_text(message):
    chat_id = message.chat.id
    session = user_data[chat_id]
    session.update({
        'caption_text': message.text,
        'state': 'awaiting_fade_choice',
        'timestamp': time.time() # <-- MODIFICATION: Update timestamp
    })
    user_data[chat_id] = session

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Yes", callback_data="fade_yes"),
        types.InlineKeyboardButton("No", callback_data="fade_no")
    )
    bot.send_message(chat_id, "Want a fade-in effect?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('fade_'))
def handle_fade_choice(call):
    chat_id = call.message.chat.id
    session = user_data.get(chat_id)
    if not session or session.get('state') != 'awaiting_fade_choice':
        bot.answer_callback_query(call.id, "This choice is no longer valid or has expired.", show_alert=True)
        bot.edit_message_text("This choice has expired. Please start over by sending media.", chat_id, call.message.message_id)
        return

    bot.answer_callback_query(call.id)
    
    apply_fade = call.data == "fade_yes"
    choice_text = "Yes" if apply_fade else "No"
    bot.edit_message_text(f"Fade-in effect: {choice_text}", chat_id, call.message.message_id)
    
    processing_message = bot.send_message(chat_id, "⚙️ Your request is in the queue...")
    
    # Start the isolated process
    p = multiprocessing.Process(
        target=isolated_video_processing_task,
        args=(
            chat_id, session['media_path'], session['media_type'],
            session['caption_text'], apply_fade, processing_message.message_id
        )
    )
    p.start()

    # Immediately clear user data after dispatching the job
    del user_data[chat_id]

@bot.message_handler(func=lambda message: True)
def handle_other_messages(message):
    bot.reply_to(message, "Please start by sending an image or a video.")


# --- Processing Functions ---
def upload_and_process(chat_id, video_path, frame_path):
    worker_url = f"{WORKER_PUBLIC_URL}/process"
    try:
        with open(frame_path, "rb") as image_file, open(video_path, 'rb') as video_file:
            image_data = base64.b64encode(image_file.read()).decode('utf-8')
            files = {
                'video': ('final_video.mp4', video_file, 'video/mp4'),
                'image_data': (None, image_data),
                'chat_id': (None, str(chat_id))
            }
            response = requests.post(worker_url, files=files, timeout=30)
            response.raise_for_status()
        logging.info("Successfully sent video and frame to worker for processing.")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Error uploading to Cloudflare Worker: {e}")
        return False

def isolated_video_processing_task(chat_id, media_path, media_type, caption_text, apply_fade, message_id):
    
    def send_bot_message(text, parse_mode=None):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
        if parse_mode: payload['parse_mode'] = parse_mode
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logging.error(f"Isolated process failed to send message: {e}")

    output_filepath = os.path.join(OUTPUT_PATH, f"output_{chat_id}.mp4")
    caption_image_path, frame_path = None, None

    try:
        send_bot_message("⚙️ Processing your video...")
        
        duration = IMAGE_DURATION
        if media_type == 'image':
            media_w, media_h = get_media_dimensions(media_path, media_type)
        else:
            media_w, media_h, duration = get_media_dimensions(media_path, media_type)

        caption_image_path, caption_height = create_caption_image(caption_text, COMP_WIDTH, chat_id)
        
        scale_ratio = COMP_WIDTH / media_w
        scaled_media_h = int(media_h * scale_ratio)
        media_y_pos = (COMP_HEIGHT - scaled_media_h) / 2 + MEDIA_Y_OFFSET
        caption_y_pos = media_y_pos - caption_height + 1

        command = ['ffmpeg', '-y', '-f', 'lavfi', '-i', f'color=c={BACKGROUND_COLOR}:s={COMP_SIZE_STR}:d={duration}']
        if media_type == 'image': command.extend(['-loop', '1', '-t', str(duration)])
        command.extend(['-i', media_path, '-i', caption_image_path])
        
        if apply_fade:
            filter_complex = (
                f"[1:v]scale={COMP_WIDTH}:-1,setpts=PTS-STARTPTS[scaled_media];"
                f"color=c=black:s={COMP_WIDTH}x{scaled_media_h+1}:d={duration}[black_layer];"
                f"[black_layer]format=rgba,fade=t=out:st=0:d={FADE_IN_DURATION}:alpha=1[fading_black_layer];"
                f"[scaled_media][fading_black_layer]overlay=0:0[media_with_fade];"
                f"[0:v][media_with_fade]overlay=(W-w)/2:{media_y_pos}[bg_with_media];"
                f"[bg_with_media][2:v]overlay=(W-w)/2:{caption_y_pos}[final_v]"
            )
        else:
            filter_complex = (
                f"[1:v]scale={COMP_WIDTH}:-1,setpts=PTS-STARTPTS[media];"
                f"[0:v][media]overlay=(W-w)/2:{media_y_pos}[bg_with_media];"
                f"[bg_with_media][2:v]overlay=(W-w)/2:{caption_y_pos}[final_v]"
            )
        
        map_args = ['-map', '[final_v]']
        if media_type == 'video':
            filter_complex += ";[1:a]asetpts=PTS-STARTPTS[final_a]"
            map_args.extend(['-map', '[final_a]'])
            
        command.extend(['-filter_complex', filter_complex, *map_args])
        command.extend([
            '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2', 
            '-c:a', 'aac', '-b:a', '192k', '-r', str(FPS), '-pix_fmt', 'yuv420p', 
            output_filepath
        ])
        
        # --- MODIFICATION: More memory-efficient subprocess call ---
        # We don't need to capture stdout, only stderr if an error occurs.
        subprocess.run(command, check=True, capture_output=True)
        logging.info("FFmpeg processing finished.")

        send_bot_message("⬆️ Uploading to our servers...")
        frame_path = extract_frame_from_video(output_filepath, duration, chat_id)
        if frame_path:
            if not upload_and_process(chat_id, output_filepath, frame_path):
                send_bot_message("⚠️ Upload to our servers failed. Please try again.")
        else:
            logging.error("Could not extract frame for processing.")
            send_bot_message("⚠️ Could not prepare video for the final step.")

        send_bot_message("✅ Done! Your video will be sent by our other systems shortly.")

    except subprocess.CalledProcessError as e:
        # The error is still captured in the exception object without holding stdout in memory.
        error_details = f"FFmpeg Error:\n`{e.stderr.decode('utf-8', 'ignore')[-1000:]}`"
        logging.error(f"FFmpeg failed for chat {chat_id}:\nSTDERR: {error_details}")
        send_bot_message(error_details, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error in isolated process: {e}", exc_info=True)
        send_bot_message("An unexpected error occurred. Please try again.")
    finally:
        cleanup_files(filter(None, [media_path, caption_image_path, output_filepath, frame_path]))

# --- Main Bot Loop ---
if __name__ == '__main__':
    logging.info("Starting bot...")
    create_directories()
    
    # --- MODIFICATION: Start the cleanup thread as a daemon ---
    # A daemon thread will exit automatically when the main program exits.
    cleanup_thread = threading.Thread(target=cleanup_stale_sessions, daemon=True)
    cleanup_thread.start()
    logging.info("Stale session cleanup thread started.")
    
    while True:
        try:
            logging.info("Bot is alive and polling for messages...")
            bot.polling(none_stop=True)
        except Exception as e:
            logging.error(f"Bot polling loop failed: {e}", exc_info=True)
            time.sleep(30) # Avoid rapid-fire crashes
