import threading, time, webbrowser
from app import app, init_db

def open_browser():
    time.sleep(1.1)
    webbrowser.open('http://127.0.0.1:5000')

if __name__ == '__main__':
    init_db()
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
