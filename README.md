Ship Proxy System

A lightweight client-server proxy system implemented in Python. This repository contains Dockerized client and server components for easy deployment and testing.

Project Structure
ship-proxy-system/
├── client/
│   ├── client.py
│   └── Dockerfile
├── server/
│   ├── server.py
│   └── Dockerfile
└── README.md

Requirements

Docker installed on your machine
Python 3.x (for local testing if not using Docker)

Setup
Build Docker Images
From the project root:
Server:
docker build -t ship-server ./server
Client:
docker build -t ship-client ./client

Run the Containers

Start the server:
docker run -d --name ship-server -p 8080:8080 ship-server
Start the client:
docker run --rm ship-client
Adjust the port if your server listens on 9999.

Usage Examples
You can interact with the server using curl:

# Basic GET request to the server
curl http://localhost:8080/

# Example POST request
curl -X POST http://localhost:8080/data -d '{"key": "value"}' -H "Content-Type: application/json"

Local Testing (Optional)
If you want to test without Docker:
Start server locally:
cd server
python3 server.py

Run client locally:
cd client
python3 client.py

Contributing

Feel free to fork and open pull requests
Ensure Docker images are tested before submitting

License
MIT License

