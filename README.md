# Stock Advisor

A personal AI-powered stock advisor that fetches financial news, scores it for credibility, and uses Claude to generate informed buy/watch/avoid recommendations with portfolio allocation suggestions.

## Features

- News ingestion from RSS feeds, Reddit, Finnhub, and SEC EDGAR
- Confidence scoring and fake news filtering
- Claude AI analysis for stock recommendations
- Portfolio calculator with weighted allocation
- Streamlit dashboard with live price data
- Position tracking with exit condition monitoring
- Automated alerts via desktop notification and email
- Google Sheets export with historical charts
- Watch list editor for stocks, ETFs, and crypto

## Setup

### 1. Clone the repo

git clone https://github.com/yourusername/stock-advisor.git
cd stock-advisor

### 2. Create virtual environment

python -m venv venv
venv\Scripts\activate

### 3. Install dependencies

pip install -r requirements.txt

### 4. Set up environment variables

Copy .env.example to .env and fill in your API keys

### 5. Set up Google Sheets

- Create a Google Cloud project and enable the Sheets API
- Create a service account and download credentials JSON
- Rename it to google_credentials.json and place in project root
- Share your Google Sheet with the service account email

### 6. Run the dashboard

streamlit run dashboard/app.py

## API Keys Required

- Anthropic — console.anthropic.com
- Finnhub — finnhub.io/register
- Google Sheets API — console.cloud.google.com
- Gmail App Password — myaccount.google.com/apppasswords

## Project Structure

```
stock-advisor/
├── ingestion/        # News fetching (RSS, Reddit, Finnhub, SEC)
├── validation/       # Confidence scoring
├── analysis/         # Claude AI analysis
├── calculator/       # Portfolio allocation math
├── storage/          # Positions, watch list, sheets, cache
├── alerts/           # Exit checker, notifications, snooze
├── dashboard/        # Streamlit UI
└── main.py           # Pipeline runner
```
