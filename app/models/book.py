from sqlalchemy import Column, Integer, String, Text
from app.core.database import Base

class Book(Base):

    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)

    source = Column(String)

    title = Column(String)

    author = Column(String)

    genre = Column(String)

    cover = Column(String)

    download = Column(String)

    language = Column(String)

    synopsis = Column(Text, nullable=True)

    chapters_count = Column(Integer, nullable=True)

    chapter_content = Column(Text, nullable=True)
