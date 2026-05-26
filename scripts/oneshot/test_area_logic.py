import sqlite3
from utils.keywords import get_next_keyword_with_area, update_keyword_area_searched

conn = sqlite3.connect(':memory:')
conn.execute('''CREATE TABLE keywords (keyword TEXT PRIMARY KEY, source TEXT NOT NULL,
    last_searched TEXT, last_new_company TEXT, total_found INTEGER DEFAULT 0, is_archived INTEGER DEFAULT 0)''')
conn.execute('''CREATE TABLE keyword_area_log (keyword TEXT NOT NULL, area_name TEXT NOT NULL,
    last_searched TEXT, PRIMARY KEY (keyword, area_name))''')

conn.execute("INSERT INTO keywords VALUES ('テスト', 'google_yahoo', NULL, NULL, 5, 0)")
conn.commit()

areas = [{'name': '東京', 'lat': 35.6}, {'name': '大阪', 'lat': 34.6}]

result = get_next_keyword_with_area(conn, 'google_yahoo', 24, areas)
print('1st pick:', result)
update_keyword_area_searched(conn, result['keyword'], result['area']['name'])

result2 = get_next_keyword_with_area(conn, 'google_yahoo', 24, areas)
print('2nd pick (different area):', result2)
update_keyword_area_searched(conn, result2['keyword'], result2['area']['name'])

result3 = get_next_keyword_with_area(conn, 'google_yahoo', 24, areas)
print('3rd pick (should be None - all cooled):', result3)

# cooling=0 should always return
result4 = get_next_keyword_with_area(conn, 'google_yahoo', 0, areas)
print('4th pick (cooling=0, should always get one):', result4)
