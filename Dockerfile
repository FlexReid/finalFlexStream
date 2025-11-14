# Use the official Playwright Python image with browsers preinstalled
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

# Set working directory
WORKDIR /app

# Copy your app files
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN pip install playwright && playwright install

# Expose the port Flask will run on
EXPOSE 5000

# Set environment variables for headless Playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Start Flask
CMD ["python", "app.py"]
