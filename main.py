from toolbox.media import YoutubeDownloader

if __name__ == '__main__':
    downloader = YoutubeDownloader(
        output_dir="/Users/zhao/Desktop/短视频/youtube",
        browser="chrome",
    )
    downloader.download("https://www.youtube.com/watch?v=LD2XmkFOuWs")
