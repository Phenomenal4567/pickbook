from sqlalchemy import Column, Integer, String
from app.core.database import Base

class Book(Base):

    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)

    source = Column(String)

    title = Column(String)

    author = Column(String)

    genre = Column(String)   # ✅ ADD THIS LINE

    cover = Column(String)

    download = Column(String)

    language = Column(String)