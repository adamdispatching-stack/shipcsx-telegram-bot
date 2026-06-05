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

# Run a HEADED Chromium under a virtual display (Xvfb). ShipCSX's Angular form
# does not render reliably in headless mode, so we drive a real browser window
# inside Xvfb. xvfb is already present in the Playwright image.
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1366x900x24", "python", "bot.py"]
