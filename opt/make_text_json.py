import csv
import json

# CSVファイルのパスを指定
csv_file_path = './title_and_textpath.csv'

# JSONファイルのパスを指定
json_file_path = './text.json'

with open(csv_file_path, 'r') as csvfile:
    csv_reader = csv.reader(csvfile)

    pages = []
    
    for row in csv_reader:
        title = row[0]
        filepath = row[1]

        page = {}
        page['title'] = title

        with open(filepath, 'r') as textfile:
            lines = [line.strip() for line in textfile]
            page['lines'] = lines

        pages.append(page)

    json.dump({'pages': pages}, open(json_file_path, 'w'))

