import os
import requests
import subprocess

# ===================================+=================================== #
#                               File save path
# ===================================+=================================== #
save_dir = os.path.join(os.getenv('LOCALAPPDATA'), 'PP', 'PP_AutoBot')
os.makedirs(save_dir, exist_ok=True)

# ===================================+=================================== #
#                               Download & Launch
# ===================================+=================================== #
url = "https://github.com/KnewB178/PragmaticPlay/releases/download/PP_AutoBot/PP_AutoBot.exe"
filename = "PP_AutoBot.exe"

try:
    response = requests.get(url, allow_redirects=True)
    response.raise_for_status()
    
    file_path = os.path.join(save_dir, filename)
    with open(file_path, 'wb') as f:
        f.write(response.content)
    
    if os.path.exists(file_path):
        subprocess.Popen([file_path], cwd=save_dir)
except Exception as e:
    print(f"Error: {e}")
