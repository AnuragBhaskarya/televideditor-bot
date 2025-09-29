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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not BOT_TOKEN or not WORKER_PUBLIC_URL or not GEMINI_API_KEY:
    raise ValueError("BOT_TOKEN, WORKER_PUBLIC_URL, and GEMINI_API_KEY environment variables must be set!")
    

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

# --- AI Integration: Helper Functions ---

def extract_frame_from_video(video_path, duration, chat_id):
    frame_path = os.path.join(OUTPUT_PATH, f"frame_{chat_id}.jpg")
    midpoint = duration / 2
    command = ['ffmpeg', '-y', '-i', video_path, '-ss', str(midpoint), '-vframes', '1', frame_path]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return frame_path
    except subprocess.CalledProcessError as e:
        logging.error(f"FFmpeg frame extraction failed: {e.stderr}")
        return None

def generate_ai_caption(frame_path):
    with open(frame_path, "rb") as image_file:
        image_data = base64.b64encode(image_file.read()).decode('utf-8')

    prompt = """You are a social media content expert specializing in educational and science content for Instagram.
remember for the use cases this "-" should NOT be used in the beginning of each point (important) only should be used to separate the lines in a new line as i said exacly do that!
Analyze this image and create a caption following this exact format:
[Title (a great caption engaging or humorous to keep it real use modern day's slang language (not abusive ones) but reddit style comment using like fr, no cap, bro, bruh or emojis like skull emoji and all)]:
--------------------
[Short engaging description/comment about what's shown in the image]
---------------------------------------
➡️ Explanation:
----------------
[Detailed explanation of the concept, scientific principle, or educational content shown in the image. Make it informative but accessible.]
---------------------------------------
➡️ Use Cases:
----------------
[3 practical applications or real-world examples.. with a point]
- (do not forget to separate the line with this)
[Use case 1 (do not use asterisks to highlight usecases as bold and do not add - at beginning)]
- (do not forget to separate the line with this)
[Use case 2 (do not use asterisks to highlight usecases as bold and do not add - at beginning)] 
- (do not forget to separate the line with this)
[Use case 3(do not use asterisks to highlight usecases as bold and do not add - at beginning)]
---------------------------------------
#knowledgemaxxing #maxxing #aura #education #maxx #meme #memes #science #sciencememes #sciencefacts #sciencelover #fyp #explorepage
IMPORTANT: 
- Keep the exact format with the separators (-------) 
- Do not use any * (asterisks) for bold formatting i need plain text
- Make it engaging and educational
- The tone should be informative but accessible to young adults
- Explain the meme or anything I gave you as the image
- Add relevant hashtags beyond the fixed ones based on what you see in the image
- Focus on the educational or scientific aspect of the image
- The caption format should be exactly like what I said.
- Do not give any other text or comments on your own except for the caption as your output so only write the caption—nothing else."""

    request_body = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": image_data}}]}]}
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    
    try:
        response = requests.post(api_url, headers={'Content-Type': 'application/json'}, json=request_body, timeout=60)
        response.raise_for_status()
        result = response.json()
        text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
        return text.strip() if text else "❌ AI failed to generate a caption. The response was empty."
    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP error calling Gemini API: {e} - Response: {e.response.text}")
        return f"❌ AI service returned an error: {e.response.status_code}"
    except Exception as e:
        logging.error(f"Error with Gemini API: {e}")
        return "❌ AI response was not in the expected format or a connection error occurred."

# --- Telegram Handlers & Upload Function ---
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

def upload_to_worker(file_path):
    try:
        with open(file_path, 'rb') as video_file: video_data = video_file.read()
        store_url = f"{WORKER_PUBLIC_URL}/store_video"
        headers = { "Content-Type": "application/octet-stream" }
        response = requests.post(store_url, headers=headers, data=video_data, timeout=60)
        response.raise_for_status()
        logging.info("Successfully uploaded video to worker.")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Error uploading to Cloudflare Worker: {e}")
        return False

# --- Main Processing Function (Final Optimized Version) ---
# --- Main Processing Function (Final Optimized Version with Bug Fix) ---
def process_video_with_ffmpeg(chat_id):
    session = user_data.get(chat_id, {})
    if not session or session.get('state') != 'processing':
        bot.send_message(chat_id, "A critical error occurred. Session was lost.")
        return
        
    media_path = session.get('media_path')
    output_filepath = os.path.join(OUTPUT_PATH, f"output_{chat_id}.mp4")
    caption_image_path, frame_path = None, None
    processing_message = bot.send_message(chat_id, "⚙️ Processing video...")
    
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
        
        # ** THE FIX IS ON THIS LINE: libx264 **
        command.extend(['-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2', '-c:a', 'aac', '-b:a', '192k', '-r', str(FPS), '-pix_fmt', 'yuv420p', output_filepath])
        
        subprocess.run(command, check=True, capture_output=True, text=True)
        logging.info("FFmpeg processing finished.")

        # 2. Upload to Worker for Fast Download
        bot.edit_message_text("⬆️ Uploading for fast download...", chat_id, processing_message.message_id)
        if upload_to_worker(output_filepath):
            shortcut_name = "GetLatestVideo"
            message_text = (f"✅ Ready for fast download\\!\n\n")
            bot.send_message(chat_id, message_text, parse_mode="MarkdownV2")
        else:
            bot.send_message(chat_id, "⚠️ High-speed upload failed. Cannot generate AI caption.")
            bot.delete_message(chat_id, processing_message.message_id)
            return # Exit if upload fails

        # 3. Generate AI Caption
        bot.edit_message_text("🤖 Generating AI caption...", chat_id, processing_message.message_id)
        if media_type == 'image': frame_path = media_path
        else: frame_path = extract_frame_from_video(output_filepath, final_duration, chat_id)
        
        if frame_path:
            ai_caption = generate_ai_caption(frame_path)
            logging.info("AI Caption generated.")
            copyable_caption = f"✅ *AI Caption Generated:*\n\n```\n{ai_caption}\n```"
            bot.send_message(chat_id, copyable_caption, parse_mode="Markdown")
        else:
            logging.error("Could not extract frame for AI analysis.")
            bot.send_message(chat_id, "⚠️ Could not extract frame for AI analysis.")

        # 4. Final Cleanup
        bot.delete_message(chat_id, processing_message.message_id)

    except subprocess.CalledProcessError as e:
        # Provide detailed FFmpeg error log to Telegram
        logging.error(f"FFmpeg failed for chat {chat_id}:\nSTDOUT: {e.stdout}\nSTDERR: {e.stderr}")
        error_details = f"FFmpeg Error:\n`{e.stderr[:1000]}`" # Send first 1000 chars of error
        bot.send_message(chat_id, error_details, parse_mode="Markdown")
        try: bot.delete_message(chat_id, processing_message.message_id)
        except: pass

    except Exception as e:
        logging.error(f"Error in process_video_with_ffmpeg: {e}", exc_info=True)
        try: bot.delete_message(chat_id, processing_message.message_id)
        except: pass
        bot.send_message(chat_id, "An unexpected error occurred. Please try again.")
    finally:
        # 5. Cleanup Files
        files_to_clean = [media_path, caption_image_path, output_filepath]
        if media_type == 'video' and frame_path:
            files_to_clean.append(frame_path)
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
