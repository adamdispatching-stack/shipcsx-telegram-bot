# Playwright's official image already includes Chromium + all system libs.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

# Unbuffered stdout/stderr so logs show up live in Railway.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Make sure the Chromium build Playwright expects is present.
RUN python -m playwright install chromium

COPY bot.py .

# Start the bot (headless Chromium). PYTHONUNBUFFERED above keeps logs live.
CMD ["python", "bot.py"]
