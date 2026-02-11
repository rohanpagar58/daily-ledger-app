# Daily Ledger App

A simple Flask application to track daily credited and debited amounts for different banks.

## Features
-   **Shop/User Authentication**: Signup and Login.
-   **Bank Management**: Add banks and separate opening balances.
-   **Daily Entries**: Record credit/debit transactions.
-   **Reports**: View Daily, Weekly, Monthly, and Custom date-range reports.

## Local Development

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Configuration**:
    -   The project uses a `.env` file for configuration.
    -   A sample `.env` file is created for you. You can change `SECRET_KEY` and `MONGO_URI` if needed.
    -   Default `MONGO_URI` is `mongodb://localhost:27017/` (requires a local MongoDB instance).

3.  **Run the App**:
    ```bash
    flask run
    ```
    Access the app at `http://127.0.0.1:5000`.

## Deployment (Render + MongoDB Atlas)

### 1. MongoDB Atlas (Database)
1.  Create a free account on [MongoDB Atlas](https://www.mongodb.com/atlas/database).
2.  Create a new Cluster (FREE tier).
3.  Create a Database User (username/password) and whitelist your IP (or allow all `0.0.0.0/0` for easiest access).
4.  Get your **Connection String** (URI). It looks like:
    `mongodb+srv://<username>:<password>@cluster0.mongodb.net/?retryWrites=true&w=majority`

### 2. Render (Web Hosting)
1.  Push this code to a GitHub repository.
2.  Create a new **Web Service** on [Render](https://render.com/).
3.  Connect your GitHub repository.
4.  Render will automatically detect the `Procfile` and `requirements.txt`.
5.  **Environment Variables**:
    -   Add `MONGO_URI`: Paste your Atlas connection string here.
    -   Add `SECRET_KEY`: Generate a random secure string.
6.  Click **Deploy**.

## Project Structure
-   `app.py`: Main application logic.
-   `templates/`: HTML files (using `base.html` for layout).
-   `requirements.txt`: Python libraries.
-   `Procfile`: Instruction for Render to run the app.

## Troubleshooting

### Incorrect Balances
If you notice that the **Opening Balance** or **Available Balance** does not match your reports or expectations (e.g., after editing an old entry):
1.  Go to the **Reports** page.
2.  Click the **Sync / Fix Balances** button at the bottom.
3.  This will recalculate all balances from the very first entry based on date and time.
