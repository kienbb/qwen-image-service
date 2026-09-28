@echo off
rem Start ComfyUI for Qwen-Image 2.1 GGUF. Add --lowvram below if you hit CUDA out-of-memory.
cd /d "%~dp0ComfyUI_windows_portable"
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --output-directory "%~dp0output" %*
pause
