import os
import redis
from dotenv import load_dotenv

# Загружаем .env (в Docker переменные передаются через env_file,
# но при локальном запуске без Docker это тоже полезно)
load_dotenv()

def main():
    # Проверяем, что видим переменные окружения
    bot_token = os.getenv("BOT_TOKEN", "")
    channel_id = os.getenv("CHANNEL_ID", "")
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
    
    print(f"BOT_TOKEN: {'установлен' if bot_token else 'НЕ УСТАНОВЛЕН'}")
    print(f"CHANNEL_ID: {channel_id}")
    print(f"OPENROUTER_API_KEY: {'установлен' if openrouter_key else 'НЕ УСТАНОВЛЕН'}")
    
    # Пробуем подключиться к Redis
    r = redis.Redis(host="redis", port=6379, decode_responses=True)
    pong = r.ping()
    print(f"Redis ping: {pong}")
    
    # Проверяем, что можем писать/читать
    r.set("test_key", "hello_newsbot")
    value = r.get("test_key")
    print(f"Redis get: {value}")
    
    print("✅ Всё работает!")

if __name__ == "__main__":
    main()
