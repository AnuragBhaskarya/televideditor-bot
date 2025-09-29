import os
import telebot
import time
import requests
from PIL import Image, ImageDraw, ImageFont
import textwrap
import subprocess
import json
import logging
import base64
import re

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


COMP_WIDTH = 1080
COMP_HEIGHT = 1920
COMP_SIZE_STR = f"{COMP_WIDTH}x{COMP_HEIGHT}"
BACKGROUND_COLOR = "black"
FPS = 30
IMAGE_DURATION = 8
FADE_IN_DURATION = 6
MEDIA_Y_OFFSET = 100
CAPTION_V_PADDING = 40
CAPTION_FONT_SIZE = 60
CAPTION_FONT = "Inter_28pt-ExtraBold"
CAPTION_TEXT_COLOR = (0, 0, 0)
CAPTION_BG_COLOR = (255, 255, 255)
DOWNLOAD_PATH = "downloads"
OUTPUT_PATH = "outputs"

# --- Bot Initialization ---
bot = telebot.TeleBot(BOT_TOKEN)
user_data = {}

# --- Helper Functions ---

def cleanup_files(file_list):
    for file_path in file_list:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logging.error(f"Error deleting file {file_path}: {e}")

def create_directories():
    if not os.path.exists(DOWNLOAD_PATH): os.makedirs(DOWNLOAD_PATH)
    if not os.path.exists(OUTPUT_PATH): os.makedirs(OUTPUT_PATH)

def get_media_dimensions(media_path, media_type):
    if media_type == 'image':
        with Image.open(media_path) as img: return img.width, img.height
    else:
        command = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,duration', '-of', 'json', media_path]
        result = subprocess.run(command, capture_output=True, text=True)
        data = json.loads(result.stdout)
        stream_data = data['streams'][0]
        return stream_data['width'], stream_data['height'], float(stream_data['duration'])

def create_caption_image(text, media_width):
    font_path = f"{CAPTION_FONT}.ttf"
    font = ImageFont.truetype(font_path, CAPTION_FONT_SIZE)
    wrapped_lines = textwrap.wrap(text, width=30, break_long_words=True)
    wrapped_text = "\n".join(wrapped_lines)
    text_bbox = ImageDraw.Draw(Image.new('RGB', (0,0))).multiline_textbbox((0, 0), wrapped_text, font=font, align="center")
    text_height = text_bbox[3] - text_bbox[1]
    rect_height = text_height + (2 * CAPTION_V_PADDING)
    img_height = int(rect_height)
    img = Image.new('RGBA', (COMP_WIDTH, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (COMP_WIDTH, img_height)], fill=CAPTION_BG_COLOR)
    draw.multiline_text((COMP_WIDTH / 2, img_height / 2), wrapped_text, font=font, fill=CAPTION_TEXT_COLOR, anchor="mm", align="center")
    caption_image_path = os.path.join(OUTPUT_PATH, f"caption_{user_data.get('chat_id', 'temp')}.png")
    img.save(caption_image_path)
    return caption_image_path, rect_height

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


# --- Telegram Handlers ---
@bot.message_handler(content_types=['photo', 'video'])
def handle_media(message):
    chat_id = message.chat.id
    if user_data.get(chat_id, {}).get('state') in ['downloading', 'processing']:
        bot.reply_to(message, "I'm currently busy. Please wait until the current process is finished.")
        return
    user_data[chat_id] = {'state': 'downloading'}
    download_message = bot.reply_to(message, "⬇️ Media detected. Starting download...")
    try:
        if message.content_type == 'photo': file_id, media_type = message.photo[-1].file_id, 'image'
        elif message.content_type == 'video': file_id, media_type = message.video.file_id, 'video'
        user_data[chat_id]['media_type'] = media_type
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        file_extension = os.path.splitext(file_info.file_path)[1]
        save_path = os.path.join(DOWNLOAD_PATH, f"{chat_id}_{file_id}{file_extension}")
        with open(save_path, 'wb') as new_file: new_file.write(downloaded_file)
        user_data[chat_id].update({'media_path': save_path, 'state': 'awaiting_caption'})
        bot.edit_message_text("✅ Media received! Now, please send the top caption text.", chat_id, download_message.message_id)
    except Exception as e:
        logging.error(f"Error in handle_media: {e}", exc_info=True)
        bot.edit_message_text(f"❌ An error occurred while processing your media.", chat_id, download_message.message_id)
        if chat_id in user_data: del user_data[chat_id]

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    chat_id = message.chat.id
    if user_data.get(chat_id, {}).get('state') == 'awaiting_caption':
        user_data[chat_id].update({'state': 'processing', 'caption': message.text})
        process_video_with_ffmpeg(chat_id)
    else: bot.reply_to(message, "Please send an image or video first.")


# --- NEW UPLOAD AND PROCESSING FUNCTION ---
def upload_and_process(chat_id, video_path, frame_path):
    """
    Uploads the final video and the frame for AI captioning in a single request.
    """
    worker_url = f"{WORKER_PUBLIC_URL}/process"
    try:
        with open(frame_path, "rb") as image_file:
            image_data = base64.b64encode(image_file.read()).decode('utf-8')

        with open(video_path, 'rb') as video_file:
            files = {
                'video': ('final_video.mp4', video_file, 'video/mp4'),
                'image_data': (None, image_data),
                'chat_id': (None, str(chat_id))
            }
            # The worker will respond quickly, but the full process runs in the background
            response = requests.post(worker_url, files=files, timeout=30)
            response.raise_for_status()
            
        logging.info("Successfully sent video and frame to worker for processing.")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Error uploading to Cloudflare Worker: {e}")
        return False
    except Exception as e:
        logging.error(f"An unexpected error occurred in upload_and_process: {e}")
        return False


# --- Main Processing Function ---
def process_video_with_ffmpeg(chat_id):
    session = user_data.get(chat_id, {})
    if not session or session.get('state') != 'processing':
        bot.send_message(chat_id, "A critical error occurred. Session was lost.")
        return

    media_path = session.get('media_path')
    output_filepath = os.path.join(OUTPUT_PATH, f"output_{chat_id}.mp4")
    caption_image_path, frame_path = None, None
    processing_message = bot.send_message(chat_id, "⚙️ Processing your video...")

    try:
        # 1. FFmpeg Video Processing
        logging.info(f"Starting video processing for chat {chat_id}")
        media_type = session['media_type']
        final_duration = IMAGE_DURATION
        if media_type == 'image': media_w, media_h = get_media_dimensions(media_path, media_type)
        else: media_w, media_h, final_duration = get_media_dimensions(media_path, media_type)

        caption_image_path, caption_height = create_caption_image(session['caption'], COMP_WIDTH)
        scale_ratio = COMP_WIDTH / media_w
        scaled_media_h = int(media_h * scale_ratio)
        media_y_pos = (COMP_HEIGHT / 2 - scaled_media_h / 2) + MEDIA_Y_OFFSET
        caption_y_pos = media_y_pos - caption_height

        command = ['ffmpeg', '-y', '-f', 'lavfi', '-i', f'color=c={BACKGROUND_COLOR}:s={COMP_SIZE_STR}:d={final_duration}']
        if media_type == 'image': command.extend(['-loop', '1', '-t', str(final_duration)])
        command.extend(['-i', media_path, '-i', caption_image_path])
        filter_complex = (f"[1:v]scale={COMP_WIDTH}:-1,setpts=PTS-STARTPTS,fade=t=in:st=0:d={FADE_IN_DURATION}[media];"
                          f"[0:v][media]overlay=(W-w)/2:{media_y_pos}[bg_with_media];"
                          f"[bg_with_media][2:v]overlay=(W-w)/2:{caption_y_pos}[final_v]")
        map_args = ['-map', '[final_v]']
        if media_type == 'video':
            filter_complex += ";[1:a]asetpts=PTS-STARTPTS[final_a]"
            map_args.extend(['-map', '[final_a]'])
        command.extend(['-filter_complex', filter_complex, *map_args])
        command.extend(['-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2', '-c:a', 'aac', '-b:a', '192k', '-r', str(FPS), '-pix_fmt', 'yuv420p', output_filepath])

        subprocess.run(command, check=True, capture_output=True, text=True)
        logging.info("FFmpeg processing finished.")

        # 2. Extract frame for AI
        bot.edit_message_text("⬆️ Uploading and processing...", chat_id, processing_message.message_id)
        frame_path = extract_frame_from_video(output_filepath, final_duration, chat_id)
        
        # 3. Upload everything to the worker
        if frame_path:
            if not upload_and_process(chat_id, output_filepath, frame_path):
                bot.send_message(chat_id, "⚠️ Upload to our servers failed. Please try again.")
        else:
            logging.error("Could not extract frame for AI analysis.")
            bot.send_message(chat_id, "⚠️ Could not prepare video for AI analysis.")

        # 4. Final Bot Message
        bot.edit_message_text("✅ Done! GC will arrive shortly.", chat_id, processing_message.message_id)

    except subprocess.CalledProcessError as e:
        logging.error(f"FFmpeg failed for chat {chat_id}:\nSTDOUT: {e.stdout}\nSTDERR: {e.stderr}")
        error_details = f"FFmpeg Error:\n`{e.stderr[:1000]}`"
        bot.edit_message_text(error_details, chat_id, processing_message.message_id, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error in process_video_with_ffmpeg: {e}", exc_info=True)
        bot.edit_message_text("An unexpected error occurred. Please try again.", chat_id, processing_-message.message_id)
    finally:
        # 5. Cleanup Files
        files_to_clean = [media_path, caption_image_path, output_filepath, frame_path]
        cleanup_files(filter(None, files_to_clean))
        if chat_id in user_data: del user_data[chat_id]

# --- "Immortal" Main Bot Loop ---
if __name__ == '__main__':
    logging.info("Starting bot...")
    create_directories()
    while True:
        try:
            logging.info("Bot is alive and polling...")
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            logging.error(f"Bot polling loop failed: {e}", exc_info=True)
            time.sleep(5)
