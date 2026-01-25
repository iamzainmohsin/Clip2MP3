import os
import re
import shutil
import yt_dlp
import concurrent.futures

youtube_regex = re.compile(r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})')

class MyLogger(object):
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        print(msg)


def progress_hook(d):
    if d['status'] == 'finished':
        title = d['info_dict'].get('title', 'Unknown Title')
        print(f"Finished downloading: {title}")


def download_video(url, download_path, format_choice, index, total):
    print(f"Downloading {index + 1}/{total}: {url}")
    # ffmpeg_path = os.path.join(os.path.dirname(__file__), 'ffmpeg', 'bin', 'ffmpeg.exe')
    
    if format_choice == 'mp4':
        format_str = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4'
        postprocessors = []
    else:
        format_str = 'bestaudio'
        postprocessors = [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
            }
        ]


    options = {
        'outtmpl': os.path.join(download_path, '%(title)s.%(ext)s'),
        'format': format_str,
        'postprocessors': postprocessors,
        'logger': MyLogger(),
        'progress_hooks': [progress_hook],
        'compat_opts': ['no-youtube-skip-dash-manifest'],
        'no_warnings': True
    }

    
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])
        
 
def main():
    input("Press Enter key to start!")

    if shutil.which("ffmpeg") is None:
        print("FFmpeg not found. Please install FFmpeg and ensure it is in your PATH.")
        return
    
    urls = []
    while True:
        get_url = input("Video URL (YouTube only) or press Enter to skip: ").strip()

        if not get_url:
            break

        if not youtube_regex.match(get_url):
            print("Invalid URL. Please enter a valid YouTube link.")
            continue
        
        urls.append(get_url)
    
    if not urls:
        print("No URLs provided. Exiting.")
        return
    
    while True:
        format_choice = input("Choose format (mp3/mp4): ").strip().lower()
        if format_choice in ('mp3', 'mp4'):
            break
        print("Invalid format. Please enter 'mp3' or 'mp4'.")

    
    
    download_path = input("Choose download destination: ").strip()
    download_path = os.path.expanduser(download_path)
    if not os.path.exists(download_path):
        os.makedirs(download_path)
    
    print(f"Starting downloads to: {download_path}")

    
    successes, failures = 0, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(download_video, url, download_path, format_choice, i, len(urls))
            for i, url in enumerate(urls)
        ]
        for f in concurrent.futures.as_completed(futures):
            try:
                f.result()
                successes += 1
            except Exception as e:
                print(f"Download error: {e}")
                failures += 1
    
    print(f"Download complete! {successes} succeeded, {failures} failed.")
    print(f"Files saved to: {download_path}")

if __name__ == "__main__":
    main()