"""SQLite上下文离开后释放连接，避免Windows文件锁及长期服务句柄泄漏。"""
import sqlite3


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()
