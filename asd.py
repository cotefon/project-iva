from markitdown import MarkItDown
import os
from dotenv import load_dotenv

load_dotenv()

FILE = os.getenv(PDF_FILE)

md = MarkItDown(enable_plugins=False)  # Set to True to enable plugins
result = md.convert("")

with open("output.md", "w", encoding="utf-8") as file:
    file.write(str(result))


# print(result.text_content)
