from pathlib import Path
import os
ROOT=Path(__file__).resolve().parent
for folder in ('logs','outputs','data/cache'):(ROOT/folder).mkdir(parents=True,exist_ok=True)
if __name__=='__main__':
    import uvicorn
    uvicorn.run('backend.app:app',host=os.getenv('HOST','127.0.0.1'),port=int(os.getenv('PORT','8765')),workers=1)
