import os
import telebot
from telebot import types # <-- IMPORTED FOR INLINE BUTTONS
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
import gc

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
# --- Using a multiprocessing-safe dictionary for user data
manager = multiprocessing.Manager()
user_data = manager.dict()


# --- Helper Functions (Mostly Unchanged) ---

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

def create_caption_image(text, media_width, chat_id):
    padded_text = ("\n" * CAPTION_TOP_PADDING_LINES) + text
    font_path = f"{CAPTION_FONT}.ttf"
    font = ImageFont.truetype(font_path, CAPTION_FONT_SIZE)
    
    final_lines = []
    for line in padded_text.split('\n'):
        wrapped_line = textwrap.wrap(line, width=30, break_long_words=True)
        if not wrapped_line:
            final_lines.append('')
        else:
            final_lines.extend(wrapped_line)
    
    wrapped_text = "\n".join(final_lines)

    text_bbox = ImageDraw.Draw(Image.new('RGB', (0,0))).multiline_textbbox(
        (0, 0), wrapped_text, font=font, align="center", spacing=CAPTION_LINE_SPACING
    )
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
        
        file_info = bot.get_file(file_id)
        
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        file_extension = os.path.splitext(file_info.file_path)[1]
        save_path = os.path.join(DOWNLOAD_PATH, f"{chat_id}_{file_id}{file_extension}")

        with requests.get(file_url, stream=True) as r:
            r.raise_for_status()
            with open(save_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        # --- MODIFICATION --- Update state and store media info
        current_data = user_data.get(chat_id, {})
        current_data.update({
            'media_path': save_path,
            'media_type': media_type,
            'state': 'awaiting_caption'
        })
        user_data[chat_id] = current_data
        bot.edit_message_text("✅ Media received! Now, please send the top caption text.", chat_id, download_message.message_id)

    except Exception as e:
        logging.error(f"Error in handle_media: {e}", exc_info=True)
        bot.edit_message_text(f"❌ An error occurred while processing your media.", chat_id, download_message.message_id)
        if chat_id in user_data: del user_data[chat_id]

@bot.message_handler(func=lambda message: user_data.get(message.chat.id, {}).get('state') == 'awaiting_caption')
def handle_text(message):
    chat_id = message.chat.id
    # --- MODIFICATION START ---
    # Store caption and ask about the fade effect
    session = user_data[chat_id]
    session['caption_text'] = message.text
    session['state'] = 'awaiting_fade_choice'
    user_data[chat_id] = session

    markup = types.InlineKeyboardMarkup()
    yes_button = types.InlineKeyboardButton("Yes", callback_data="fade_yes")
    no_button = types.InlineKeyboardButton("No", callback_data="fade_no")
    markup.add(yes_button, no_button)
    
    bot.send_message(chat_id, "Want fade-in effect?", reply_markup=markup)
    # --- MODIFICATION END ---

# --- NEW: CALLBACK HANDLER FOR FADE-IN CHOICE ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('fade_'))
def handle_fade_choice(call):
    chat_id = call.message.chat.id
    if user_data.get(chat_id, {}).get('state') != 'awaiting_fade_choice':
        bot.answer_callback_query(call.id, "This choice is no longer valid.", show_alert=True)
        return

    # Acknowledge the button press
    bot.answer_callback_query(call.id)
    
    # Determine user's choice
    apply_fade = call.data == "fade_yes"
    
    session = user_data[chat_id]
    
    # Edit the original message to remove the buttons and show the choice
    choice_text = "Yes" if apply_fade else "No"
    bot.edit_message_text(f"Fade-in effect: {choice_text}", chat_id, call.message.message_id)
    
    processing_message = bot.send_message(chat_id, "⚙️ Your request is in the queue...")
    
    # Start the background process with the fade choice
    p = multiprocessing.Process(
        target=isolated_video_processing_task,
        args=(
            chat_id,
            session['media_path'],
            session['media_type'],
            session['caption_text'],
            apply_fade, # <-- Pass the new argument
            processing_message.message_id
        )
    )
    p.start()

    # Immediately clear the user data from the main process's memory
    del user_data[chat_id]

@bot.message_handler(func=lambda message: True)
def handle_other_messages(message):
    bot.reply_to(message, "Please send an image or video first.")
    gc.collect()


# --- UPLOAD FUNCTION (Unchanged) ---
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
    except Exception as e:
        logging.error(f"An unexpected error occurred in upload_and_process: {e}")
        return False

# --- MODIFIED: ISOLATED PROCESSING FUNCTION ---
# This function now accepts 'apply_fade' to conditionally build the FFmpeg command.
def isolated_video_processing_task(chat_id, media_path, media_type, caption_text, apply_fade, message_id):
    
    def send_bot_message(text, parse_mode=None):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
        if parse_mode:
            payload['parse_mode'] = parse_mode
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logging.error(f"Isolated process failed to send message: {e}")

    output_filepath = os.path.join(OUTPUT_PATH, f"output_{chat_id}.mp4")
    caption_image_path, frame_path = None, None

    try:
        send_bot_message("⚙️ Processing your video...")
        
        final_duration = IMAGE_DURATION
        if media_type == 'image':
            media_w, media_h = get_media_dimensions(media_path, media_type)
        else:
            media_w, media_h, final_duration = get_media_dimensions(media_path, media_type)

        caption_image_path, caption_height = create_caption_image(caption_text, COMP_WIDTH, chat_id)
        scale_ratio = COMP_WIDTH / media_w
        scaled_media_h = int(media_h * scale_ratio)
        media_y_pos = (COMP_HEIGHT / 2 - scaled_media_h / 2) + MEDIA_Y_OFFSET
        caption_y_pos = media_y_pos - caption_height + 1

        command = ['ffmpeg', '-y', '-f', 'lavfi', '-i', f'color=c={BACKGROUND_COLOR}:s={COMP_SIZE_STR}:d={final_duration}']
        if media_type == 'image': command.extend(['-loop', '1', '-t', str(final_duration)])
        command.extend(['-i', media_path, '-i', caption_image_path])
        
        # --- MODIFICATION START: Conditional Fade-in Filter using Black Overlay ---
        if apply_fade:
            filter_complex = (
                # 1. Scale the media, same as before.
                f"[1:v]scale={COMP_WIDTH}:-1,setpts=PTS-STARTPTS[scaled_media];"
                
                # 2. Create a black color source with the *exact* scaled media dimensions.
                f"color=c=black:s={COMP_WIDTH}x{scaled_media_h+1}:d={final_duration}[black_layer];"
                
                # 3. THE CRUCIAL FIX: Give the black layer an alpha channel, THEN fade it out.
                f"[black_layer]format=rgba,fade=t=out:st=0:d={FADE_IN_DURATION}[fading_black_layer];"
                
                # 4. Overlay the fading black layer on top of the scaled media.
                f"[scaled_media][fading_black_layer]overlay=0:0[media_with_fade];"
                
                # 5. Overlay the result (media + fade effect) onto the main background.
                f"[0:v][media_with_fade]overlay=(W-w)/2:{media_y_pos}[bg_with_media];"
                
                # 6. Overlay the caption on top of everything.
                f"[bg_with_media][2:v]overlay=(W-w)/2:{caption_y_pos}[final_v]"
            )
        else:
            # This is the original logic for when there is no fade. It remains unchanged.
            filter_complex = (f"[1:v]scale={COMP_WIDTH}:-1,setpts=PTS-STARTPTS[media];"
                              f"[0:v][media]overlay=(W-w)/2:{media_y_pos}[bg_with_media];"
                              f"[bg_with_media][2:v]overlay=(W-w)/2:{caption_y_pos}[final_v]")
        # --- MODIFICATION END ---
        
        map_args = ['-map', '[final_v]']
        if media_type == 'video':
            filter_complex += ";[1:a]asetpts=PTS-STARTPTS[final_a]"
            map_args.extend(['-map', '[final_a]'])
        command.extend(['-filter_complex', filter_complex, *map_args])
        command.extend(['-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2', '-c:a', 'aac', '-b:a', '192k', '-r', str(FPS), '-pix_fmt', 'yuv420p', output_filepath])
        
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        logging.info("FFmpeg processing finished.")

        send_bot_message("⬆️ Uploading and processing...")
        frame_path = extract_frame_from_video(output_filepath, final_duration, chat_id)
        if frame_path:
            if not upload_and_process(chat_id, output_filepath, frame_path):
                send_bot_message("⚠️ Upload to our servers failed. Please try again.")
        else:
            logging.error("Could not extract frame for AI analysis.")
            send_bot_message("⚠️ Could not prepare video for AI analysis.")

        send_bot_message("✅ Done! Your video and caption will arrive shortly.")

    except subprocess.CalledProcessError as e:
        logging.error(f"FFmpeg failed for chat {chat_id}:\nSTDERR: {e.stderr}")
        error_details = f"FFmpeg Error:\n`{e.stderr[:1000]}`"
        send_bot_message(error_details, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error in isolated process: {e}", exc_info=True)
        send_bot_message("An unexpected error occurred. Please try again.")
    finally:
        files_to_clean = [media_path, caption_image_path, output_filepath, frame_path]
        cleanup_files(filter(None, files_to_clean))

# --- "Immortal" Main Bot Loop ---
if __name__ == '__main__':
    logging.info("Starting bot...")
    create_directories()
    while True:
        try:
            logging.info("Bot is alive and polling...")
            bot.polling(none_stop=True)
        except Exception as e:
            logging.error(f"Bot polling loop failed: {e}", exc_info=True)
            time.sleep(5)
