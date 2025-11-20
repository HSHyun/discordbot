import psycopg2
from psycopg2.extras import RealDictCursor
from main import DB_CONFIG

def analyze_scores():
    # 현재 적용된 쿼리 로직 그대로 실행 (레딧 5배 가중치)
    query = """
        SELECT
            src.name AS source_name,
            i.title,
            COALESCE((i.metadata->>'views')::int, 0) as views,
            COALESCE((i.metadata->>'recommends')::int, 0) as recommends,
            COALESCE((i.metadata->>'score')::int, 0) as score,
            (
                COALESCE((i.metadata->>'recommends')::int, 0) * 50 + 
                COALESCE((i.metadata->>'views')::int, 1) +
                COALESCE((i.metadata->>'score')::int, 0) * 5
            ) as calculated_total_score
        FROM item_summary s
        JOIN item i ON i.id = s.item_id
        JOIN source src ON src.id = i.source_id
        WHERE s.updated_at >= NOW() - INTERVAL '6 hours'
        ORDER BY calculated_total_score DESC
        LIMIT 20
    """
    
    print("--- Top 20 Posts Analysis (Current Weight: Reddit * 5) ---")
    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            rows = cur.fetchall()
            
            for idx, row in enumerate(rows, 1):
                print(f"{idx}. [{row['source_name']}] {row['title'][:30]}...")
                print(f"   Total: {row['calculated_total_score']} (Rec:{row['recommends']}*50 + View:{row['views']} + Score:{row['score']}*5)")

if __name__ == "__main__":
    analyze_scores()

