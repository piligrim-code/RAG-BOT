- Project Overview
RAG Sales Assistant is a local AI-powered sales bot with:

PostgreSQL product/customer database

Semantic search via ChromaDB

LLM responses using Llama3 (via llama.cpp)

Async processing with RabbitMQ

- Key Features
 Hybrid Search - Combines SQL queries + vector search
 Local LLM - Runs Llama3 offline via llama.cpp
 Async Tasks - RabbitMQ for scalable processing
 Sales Optimized - Product recommendations, customer insights

🛠 Tech Stack
Core: Python 3.8+
Database: PostgreSQL + ChromaDB
LLM: Llama3 via llama.cpp
Messaging: RabbitMQ
Frameworks: FastAPI (optional)
API/GUI: Telegram 

<img width="2258" height="766" alt="deepseek_mermaid_20250803_c10935" src="https://github.com/user-attachments/assets/0790a78c-2405-452f-9d6f-010cab2c7e30" />

🔧 Requirements
PostgreSQL 12+ with sales data
llama.cpp compatible GPU or CPU
RabbitMQ server
ChromaDB instance
