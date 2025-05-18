from dotenv import load_dotenv
import os

BASEDIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASEDIR,'.env'),override=True)

TOKEN=os.getenv('TOKEN')
YTDLP_PATH=os.getenv('YTDLP_PATH')

