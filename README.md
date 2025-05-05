# Whoop Data Exporter for InfluxDB & Grafana

This Python script fetches data from the Whoop API using the password grant method and writes it to an InfluxDB database for visualization in Grafana.

## Setup

1.  **Clone Repository:**
    ```bash
    git clone <your-repo-url>
    cd <your-repo-directory>
    ```

2.  **Install Python Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Set up InfluxDB:**
    *   Install and run InfluxDB (Docker recommended):
        ```bash
        docker run -d --name influxdb -p 8086:8086 -v influxdb_data:/var/lib/influxdb2 influxdb:2.7
        ```
    *   Access the UI at `http://localhost:8086`.
    *   Complete the initial setup (create User, Org, Bucket). Note these details.
    *   Generate an API Token with write access to your bucket.

4.  **Configure Environment:**
    *   Copy `.env.example` to `.env` (We will create `.env.example` next).
    *   Edit `.env` and fill in your:
        *   `WHOOP_USERNAME` (your Whoop email)
        *   `WHOOP_PASSWORD` (your Whoop password)
        *   `INFLUXDB_URL` (e.g., `http://localhost:8086`)
        *   `INFLUXDB_TOKEN` (the token you generated)
        *   `INFLUXDB_ORG` (your InfluxDB organization name/ID)
        *   `INFLUXDB_BUCKET` (the bucket name you created, e.g., `whoop`)

5.  **Run Initial Data Sync:**
    ```bash
    python whoop_connect.py
    ```

6.  **Set up Grafana:**
    *   Install and run Grafana (Docker recommended):
        ```bash
        # Create network if it doesn't exist
        docker network create whoop-grafana-net || true
        # Connect InfluxDB to network
        docker network connect whoop-grafana-net influxdb || true
        # Run Grafana and connect to network
        docker run -d --name grafana -p 3000:3000 --network whoop-grafana-net -v grafana_data:/var/lib/grafana grafana/grafana-oss:latest
        ```
    *   Access Grafana at `http://localhost:3000` (login: admin/admin, change password).
    *   Add InfluxDB as a data source:
        *   URL: `http://influxdb:8086`
        *   Query Language: Flux
        *   Fill in Org, Token, Default Bucket from your `.env` file.
        *   Save & Test.

7.  **Create Dashboards:** Build panels in Grafana querying the `whoop_*` measurements from your InfluxDB data source.

8.  **Schedule Sync (Optional):**
    *   Set up a cron job (or similar scheduler) to run `python /path/to/whoop_connect.py` periodically (e.g., every 6 hours). Example crontab line:
        ```crontab
        0 */6 * * * /usr/bin/python3.9 /full/path/to/whoop_connect.py >> /full/path/to/whoop_cron.log 2>&1
        ```
        *(Ensure Python path and script path are correct)*# whoop2influx
# whoop2influx
