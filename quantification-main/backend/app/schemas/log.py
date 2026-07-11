from pydantic import BaseModel


class LogEntryResponse(BaseModel):
    timestamp: str
    level: str
    message: str
