@echo off
rem Start ComfyUI for Qwen-Image 2.1 GGUF. --reserve-vram keeps ~1.2 GB free so ComfyUI offloads by itself
rem instead of the Windows driver spilling into shared memory (896-1024 px went from ~40 s to 160+ s without it).
cd /d "%~dp0ComfyUI_windows_portable"
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --reserve-vram 1.2 --output-directory "%~dp0output" %*
pause
