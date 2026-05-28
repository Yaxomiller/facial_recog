import uvicorn
from src.web_config import get_web_host, get_web_port


if __name__ == "__main__":
    uvicorn.run("src.api_v2:app", host=get_web_host(), port=get_web_port(), reload=False)
