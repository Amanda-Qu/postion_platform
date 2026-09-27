"""Single-process runner: scheduler remains alive after the browser is closed."""
import os
from pathlib import Path
from dotenv import load_dotenv
import uvicorn

if __name__ == '__main__':
    load_dotenv(Path(__file__).parent / '.env')
    # Multiple uvicorn workers would own competing schedulers. Personal deployment
    # intentionally uses a single process with SQLite transactions and one queue.
    uvicorn.run('app.main:app',host=os.getenv('APP_HOST','127.0.0.1'),port=int(os.getenv('APP_PORT','8765')),workers=1,access_log=False)
