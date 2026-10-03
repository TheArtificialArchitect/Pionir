"""The video channels: a niche is config, a video is a checked script, a voice and a render.

Nothing here uploads, and nothing here takes the GPU: words come from a local model on the
CPU, narration is Kokoro on the CPU, assembly is ffmpeg. A finished video is a folder (the
*package*) that waits for the owner; ``video.youtube_upload`` is the only way out and it
parks for approval on every call.
"""
