import os
import telebot
import time
import requests
from PIL import Image, ImageDraw, ImageFont
import textwrap
import subprocess
import json
import logging # <--- NEW: Import logging module

# --- Logging Configuration ---
# This will make logs show up properly in Railway
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
MEDIA_Y_OFFSET = 50
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
    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)
    if not os.path.exists(OUTPUT_PATH):
        os.makedirs(OUTPUT_PATH)

# (The rest of your helper functions: get_media_dimensions, create_caption_image are unchanged)
def get_media_dimensions(media_path, media_type):
    if media_type == 'image':
        with Image.open(media_path) as img:
            return img.width, img.height
    else:
        command = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,duration', '-of', 'json', media_path]
        result = subprocess.run(command, capture_output=True, text=True)
        data = json.loads(result.stdout)
        stream_data = data['streams'][0]
        return stream_data['width'], stream_data['height'], float(stream_data['duration'])

def create_caption_image(text, media_width):
    font_path = f"{CAPTION_FONT}.ttf" if not CAPTION_FONT.endswith('.ttf') else CAPTION_FONT
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

# --- Telegram Handlers (Unchanged) ---
@bot.message_handler(content_types=['photo', 'video'])
def handle_media(message):
    chat_id = message.chat.id
    # ... (rest of the function is the same)
    session = user_data.get(chat_id, {})
    if session.get('state') in ['downloading', 'processing']:
        bot.reply_to(message, "I'm currently busy with your last request. Please wait until it's finished!")
        return
    user_data[chat_id] = {'state': 'downloading'}
    download_message = bot.reply_to(message, "⬇️ Media detected. Starting download...")
    try:
        if message.content_type == 'photo':
            file_id = message.photo[-1].file_id
            media_type = 'image'
        elif message.content_type == 'video':
            file_id = message.video.file_id
            media_type = 'video'
        user_data[chat_id]['media_type'] = media_type
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        file_extension = os.path.splitext(file_info.file_path)[1]
        local_filename = f"{chat_id}_{file_id}{file_extension}"
        save_path = os.path.join(DOWNLOAD_PATH, local_filename)
        with open(save_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        user_data[chat_id]['media_path'] = save_path
        user_data[chat_id]['state'] = 'awaiting_caption'
        bot.edit_message_text("✅ Media received! Now, please send the top caption text.", chat_id, download_message.message_id)
    except Exception as e:
        logging.error(f"An error occurred in handle_media for chat {chat_id}: {e}")
        bot.edit_message_text(f"❌ An error occurred while processing your media. Please try sending it again.", chat_id, download_message.message_id)
        if chat_id in user_data:
            del user_data[chat_id]


@bot.message_handler(func=lambda message: True)
def handle_text(message):
    chat_id = message.chat.id
    # ... (rest of the function is the same)
    session = user_data.get(chat_id, {})
    current_state = session.get('state')
    if current_state == 'awaiting_caption':
        user_data[chat_id]['state'] = 'processing'
        user_data[chat_id]['caption'] = message.text
        process_video_with_ffmpeg(chat_id)
    elif current_state == 'downloading':
        bot.reply_to(message, "Please wait for the media to finish downloading before sending the caption.")
    elif current_state == 'processing':
        bot.reply_to(message, "I'm already processing your video. Please wait a moment!")
    else:
        bot.reply_to(message, "Please send an image or video first before sending a caption.")


# --- Upload Function (Unchanged) ---
def upload_to_worker(file_path):
    """Uploads the video file to the Cloudflare Worker's /store_video endpoint."""
    try:
        with open(file_path, 'rb') as video_file:
            video_data = video_file.read()
        store_url = f"{WORKER_PUBLIC_URL}/store_video"
        headers = { "Content-Type": "application/octet-stream" }
        response = requests.post(store_url, headers=headers, data=video_data)
        response.raise_for_status()
        logging.info("Successfully uploaded video to worker.")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Error uploading to Cloudflare Worker: {e} - {e.response.text}")
        return False

# --- Main Processing Function (MODIFIED WITH LOGGING) ---
def process_video_with_ffmpeg(chat_id):
    session = user_data.get(chat_id)
    if not session or session.get('state') != 'processing':
        bot.send_message(chat_id, "A critical error occurred. Session was lost. Please start over.")
        return
        
    media_path = session.get('media_path')
    media_type = session.get('media_type')
    caption_text = session.get('caption')
    output_filepath = os.path.join(OUTPUT_PATH, f"output_{chat_id}.mp4")
    caption_image_path = None
    
    if not media_path:
        bot.send_message(chat_id, "Error: Media file path was not found. Please start over.")
        if chat_id in user_data: del user_data[chat_id]
        return
        
    processing_message = bot.send_message(chat_id, "⚙️ Processing your video...")
    
    try:
        logging.info(f"Starting video processing for chat {chat_id}")
        if media_type == 'image':
            media_w, media_h = get_media_dimensions(media_path, media_type)
            final_duration = IMAGE_DURATION
        else:
            media_w, media_h, final_duration = get_media_dimensions(media_path, media_type)
            
        caption_image_path, caption_height = create_caption_image(caption_text, COMP_WIDTH)
        scale_ratio = COMP_WIDTH / media_w
        scaled_media_h = int(media_h * scale_ratio)
        media_y_pos = (COMP_HEIGHT / 2 - scaled_media_h / 2) + MEDIA_Y_OFFSET
        caption_y_pos = media_y_pos - caption_height
        
        # ** A potential workaround: reduce threads to lower CPU/RAM spike **
        command = ['ffmpeg', '-y', '-f', 'lavfi', '-i', f'color=c={BACKGROUND_COLOR}:s={COMP_SIZE_STR}:d={final_duration}']
        if media_type == 'image':
             command.extend(['-loop', '1', '-t', str(final_duration)])
        command.extend(['-i', media_path, '-i', caption_image_path])
        filter_complex = (
            f"[1:v]scale={COMP_WIDTH}:-1,setpts=PTS-STARTPTS,fade=t=in:st=0:d={FADE_IN_DURATION}[media];"
            f"[0:v][media]overlay=(W-w)/2:{media_y_pos}[bg_with_media];"
            f"[bg_with_media][2:v]overlay=(W-w)/2:{caption_y_pos}[final_v]"
        )
        map_args = ['-map', '[final_v]']
        if media_type == 'video':
            filter_complex += ";[1:a]asetpts=PTS-STARTPTS[final_a]"
            map_args.extend(['-map', '[final_a]'])
            
        command.extend(['-filter_complex', filter_complex, *map_args])
        command.extend(['-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2', '-c:a', 'aac', '-b:a', '192k', '-r', str(FPS), '-pix_fmt', 'yuv420p', output_filepath])
        
        logging.info("Running FFmpeg command...")
        subprocess.run(command, check=True, capture_output=True, text=True)
        logging.info("FFmpeg processing finished successfully.")

        bot.edit_message_text("⬆️ Uploading for fast download...", chat_id, processing_message.message_id)
        success = upload_to_worker(output_filepath)
        
        bot.delete_message(chat_id, processing_message.message_id)

        if success:
            shortcut_name = "GetLatestVideo" 
            shortcut_url_scheme = f"shortcuts://run-shortcut?name={shortcut_name}"
            
            message_text = (
                "✅ Ready for fast download\\!\n\n"
                f"Tap the link below to save to Photos:\n"
                f"`{shortcut_url_scheme}`"
            )
            bot.send_message(chat_id, message_text, parse_mode="MarkdownV2")
            bot.send_video(chat_id, open(output_filepath, 'rb'), caption="Telegram preview:")
        else:
            bot.send_message(chat_id, "❌ High-speed upload failed. Sending to Telegram directly.")
            bot.send_video(chat_id, open(output_filepath, 'rb'))

    except subprocess.CalledProcessError as e:
        logging.error(f"FFmpeg failed for chat {chat_id}:\nSTDOUT: {e.stdout}\nSTDERR: {e.stderr}")
        bot.send_message(chat_id, "An error occurred during video encoding.")

    except Exception as e:
        logging.error(f"A critical error occurred in process_video_with_ffmpeg: {e}", exc_info=True)
        try:
            bot.delete_message(chat_id, processing_message.message_id)
        except telebot.apihelper.ApiException:
            pass
        bot.send_message(chat_id, f"An unexpected error occurred. Please try again.")
    finally:
        cleanup_files([media_path, caption_image_path, output_filepath])
        if chat_id in user_data:
            del user_data[chat_id]

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
