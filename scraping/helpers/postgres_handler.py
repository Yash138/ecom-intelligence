import psycopg2
from psycopg2.extras import RealDictCursor, execute_values


class PostgresDBHandler:
    def __init__(self, host, database, user, password, port=5432):
        self.host = host
        self.database = database
        self.user = user
        self.password = password
        self.port = port
        self.connection = None

    def connect(self):
        if self.connection is None or self.connection.closed != 0:
            self.connection = psycopg2.connect(
                host=self.host,
                database=self.database,
                user=self.user,
                password=self.password,
                port=self.port,
            )
        return self.connection

    def close(self):
        if self.connection and self.connection.closed == 0:
            self.connection.close()
            self.connection = None

    def insert(self, table, data):
        columns = ', '.join(data.keys())
        values = ', '.join(['%s'] * len(data))
        query = f"INSERT INTO {table} ({columns}) VALUES ({values});"
        with self.connection.cursor() as cur:
            cur.execute(query, tuple(data.values()))
        self.connection.commit()

    def read(self, table=None, columns='*', conditions=None, query=None):
        if not query:
            if isinstance(columns, list):
                columns = ', '.join(columns)
            query = f"SELECT {columns} FROM {table}"
            if conditions:
                query += f" WHERE {conditions}"
        with self.connection.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            return cur.fetchall()

    def stream_read(self, table=None, columns='*', conditions=None, query=None, batch_size=1000):
        if not query:
            if isinstance(columns, list):
                columns = ', '.join(columns)
            query = f"SELECT {columns} FROM {table}"
            if conditions:
                query += f" WHERE {conditions}"
        with self.connection.cursor(name="stream_cursor", cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    yield row

    def update(self, table, data, conditions):
        set_clause = ', '.join([f"{col} = %s" for col in data.keys()])
        conditions_clause = ' AND '.join([f"{col} = '{v}'" for col, v in conditions.items()])
        query = f"UPDATE {table} SET {set_clause} WHERE {conditions_clause}"
        with self.connection.cursor() as cur:
            cur.execute(query, tuple(data.values()))
        self.connection.commit()

    def bulk_upsert(self, table, data, conflict_columns, update_columns=None):
        if not data:
            return
        columns = list(data[0].keys())
        values = [tuple(r[c] for c in columns) for r in data]
        conflict_clause = ", ".join(conflict_columns)
        if update_columns:
            update_clause = ", ".join([f"{c} = EXCLUDED.{c}" for c in update_columns])
        else:
            update_clause = ", ".join(
                [f"{c} = EXCLUDED.{c}" for c in columns if c not in conflict_columns]
            )
        query = f"""
            INSERT INTO {table} ({', '.join(columns)})
            VALUES %s
            ON CONFLICT ({conflict_clause})
            DO UPDATE SET {update_clause};
        """
        try:
            with self.connection.cursor() as cur:
                execute_values(cur, query, values)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def execute(self, query, params=None):
        try:
            with self.connection.cursor() as cur:
                cur.execute(query, params)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
