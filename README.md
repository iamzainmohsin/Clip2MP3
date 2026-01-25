# Clip2MP3

A lightweight, cross-platform CLI tool built with Python and yt-dlp for high-quality YouTube media extraction.

Clip2MP3 streamlines the process of downloading YouTube content, allowing users to choose between high-definition MP4 video or crystal-clear MP3 audio extraction powered by the industry-standard FFmpeg engine.

## Features

- Dual Mode: Download full MP4 videos or extract high-bitrate MP3 audio.
    
- Batch Processing: Supports multiple downloads in parallel for efficiency.
    
- Terminal UI: Clean, readable output with progress tracking.
    
- Cross-Platform: Native support for macOS and Windows.
    
- Lightweight: No bloated binaries; utilizes system-level FFmpeg for maximum performance.
    

## Requirements

### System Dependencies

- Python 3.9+
    
- FFmpeg: Must be installed and accessible via your system's PATH.
    

### Python Dependencies

- yt-dlp
    

## Installation

### 1. Clone the Repository

```
git clone [https://github.com/yourusername/clip2mp3.git](https://github.com/yourusername/clip2mp3.git)
cd clip2mp3
```

### 2. Install Python Packages

```
pip install -r requirements.txt
```

### 3. Setup FFmpeg

#### macOS (via Homebrew)

```
brew install ffmpeg
```

#### Windows

1. Download the latest build from [Gyan.dev](https://www.gyan.dev/ffmpeg/builds/ "null").
    
2. Extract the archive to a permanent location (e.g., C:\ffmpeg).
    
3. Add the bin folder to your System Environment Variables (PATH).
    

> Verification: Run `ffmpeg -version` in your terminal to ensure it is correctly installed.

## Usage

To launch the interactive downloader, run:

```
python main.py
```

Follow the on-screen prompts to paste your YouTube URL and select your preferred output format.

## Contributing

Contributions make the open-source community an amazing place to learn, inspire, and create. Any contributions you make are greatly appreciated.

1. Fork the Project
    
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
    
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
    
4. Push to the Branch (`git push origin feature/AmazingFeature`)
    
5. Open a Pull Request
    

## License

Distributed under the GNU Affero General Public License v3.0. See `LICENSE` for more information.
