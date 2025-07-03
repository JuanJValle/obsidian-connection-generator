import sys
sys.path.append('/home/juan/.local/share/pipx/venvs/pip/lib/python3.12/site-packages')
import os
import sqlite3
from collections import Counter
import nltk
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize

# --- NLTK Data Download (if not present) ---
# Ensure necessary NLTK data is available. This will download them if they aren't.
# Explicitly download punkt_tab as suggested by previous errors
print("Ensuring NLTK 'stopwords' corpus is available...")
try:
    nltk.data.find('corpora/stopwords')
except LookupError:
    nltk.download('stopwords')

print("Ensuring NLTK 'punkt' tokenizer is available...")
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt')

print("Ensuring NLTK 'punkt_tab' is available (as per error suggestions)...")
try:
    # punkt_tab is typically a sub-resource of punkt, but explicit download requested by NLTK
    nltk.data.find('tokenizers/punkt_tab')
except LookupError:
    nltk.download('punkt_tab')


# --- Configuration ---
DB_NAME = 'obsidian_linker.db'
# Minimum number of shared keywords between two notes to consider them related
MIN_SHARED_KEYWORDS_FOR_CONNECTION = 3  # temporarily set to 2.  It was 3

# Number of top keywords to extract from each note's content
NUM_KEYWORDS_PER_NOTE = 20  # it was 10
# Standard English stopwords, can be extended if needed
STOP_WORDS = set(stopwords.words('english'))

def sanitize_filename(filename):
    """Removes the .md extension and prepares filename for linking/display."""
    return filename.replace('.md', '')

def get_keywords_from_text(text, num_keywords=NUM_KEYWORDS_PER_NOTE):
    """
    Extracts top N keywords from text using NLTK.
    Filters out stopwords, non-alphanumeric tokens, and very short words.
    """
    tokens = word_tokenize(text.lower())

    # Filter out non-alphanumeric tokens, stopwords, and words shorter than 3 characters
    filtered_tokens = [
        word for word in tokens
        if word.isalnum() and word not in STOP_WORDS and len(word) > 2
    ]

    # Get frequency distribution and return the most common keywords
    fdist = Counter(filtered_tokens)
    return [word for word, freq in fdist.most_common(num_keywords)]

def setup_database():
    """Sets up the SQLite database and table for storing note information."""
    print(f"Attempting to set up database at: {os.path.abspath(DB_NAME)}")
    conn = None # Initialize conn to None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filepath TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                folder_topic TEXT,
                keywords TEXT -- comma-separated string of keywords
            )
        ''')
        conn.commit()
        print("Database setup complete.")
    except sqlite3.Error as e:
        print(f"Error setting up database: {e}")
    finally:
        if conn: # Ensure connection is closed even if an error occurs
            conn.close()

def process_vault(vault_path):
    """
    Scans the Obsidian vault, extracts information from each Markdown note,
    and populates the database with note details, including keywords.
    """
    conn = None # Initialize conn to None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        print(f"Scanning vault: {vault_path}...")
        processed_files_count = 0
        for root, _, files in os.walk(vault_path):
            # Use the immediate parent folder name as the high-level topic
            folder_topic = os.path.basename(root)

            for file in files:
                if file.endswith('.md'):
                    filepath = os.path.join(root, file)
                    filename_without_ext = sanitize_filename(file)

                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            content = f.read()

                        keywords = get_keywords_from_text(content)
                        keywords_str = ','.join(keywords)

                        # Insert or replace note data in the database
                        cursor.execute(
                            "INSERT OR REPLACE INTO notes (filepath, filename, folder_topic, keywords) VALUES (?, ?, ?, ?)",
                            (filepath, filename_without_ext, folder_topic, keywords_str)
                        )
                        processed_files_count += 1
                        # print(f"  Processed: {filename_without_ext}") # Keep this commented for less verbose output during scan
                    except Exception as e: # Catch any exception during file reading or DB insertion
                        print(f"  Error processing {filepath}: {e}")

        conn.commit()
        print(f"Vault scanning complete. Processed {processed_files_count} Markdown files and populated database.")
    except sqlite3.Error as e:
        print(f"Error during vault processing and database population: {e}")
    finally:
        if conn:
            conn.close()


def create_connections_and_tags():
    """
    Reads all notes from the database, identifies connections based on shared keywords,
    and then updates the actual Markdown files with backlinks and relevant tags.
    """
    conn = None # Initialize conn to None
    all_notes_data = []
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT id, filepath, filename, folder_topic, keywords FROM notes")
        all_notes_data = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"Error reading notes from database: {e}")
        return # Exit if we can't read notes
    finally:
        if conn:
            conn.close()

    # Map to store note data and proposed changes
    note_map = {}
    if not all_notes_data:
        print("  No notes retrieved from the database. Cannot generate connections and tags.")
        print("  Please ensure 'process_vault' successfully scanned your vault and populated the database.")
        return # Exit if no notes were retrieved from DB

    for note_id, filepath, filename, folder_topic, keywords_str in all_notes_data:
        keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]
        note_map[note_id] = {
            'filepath': filepath,
            'filename': filename,
            'folder_topic': folder_topic,
            'keywords': set(keywords), # Use set for efficient intersection checks
            'proposed_backlinks': set(),
            'proposed_tags': set()
        }
        # Add folder topic and extracted keywords as initial tags for each note
        if folder_topic:
            # Sanitize folder name for tag (Obsidian prefers no spaces, lowercase)
            note_map[note_id]['proposed_tags'].add(f"#{folder_topic.replace(' ', '_').lower()}")
        for k in keywords:
            # Sanitize keywords for tags
            note_map[note_id]['proposed_tags'].add(f"#{k.replace(' ', '_').lower()}")

    print("Generating connections and tags based on shared ideas...")
    note_ids = list(note_map.keys())
    if not note_ids:
        print("  No notes found in the note_map after processing database results. This should not happen if notes were retrieved.")
        return

    # Compare each note with every other note to find commonalities
    connections_found_count = 0
    for i in range(len(note_ids)):
        note1_id = note_ids[i]
        note1 = note_map[note1_id]

        for j in range(i + 1, len(note_ids)): # Avoid duplicate comparisons (note A vs B, B vs A)
            note2_id = note_ids[j]
            note2 = note_map[note2_id]

            shared_keywords = note1['keywords'].intersection(note2['keywords'])
            print(f"  Comparing '{note1['filename']}' and '{note2['filename']}'. Shared keywords count: {len(shared_keywords)}")

            # If a sufficient number of keywords are shared, establish a connection
            if len(shared_keywords) >= MIN_SHARED_KEYWORDS_FOR_CONNECTION:
                print(f"    --> Connection found! Shared keywords: {list(shared_keywords)}")
                connections_found_count += 1
                # Add backlinks to both notes
                note1['proposed_backlinks'].add(f"[[{note2['filename']}]]")
                note2['proposed_backlinks'].add(f"[[{note1['filename']}]]")

                # Add shared keywords as tags to both notes, reinforcing the connection
                for keyword in shared_keywords:
                    tag_name = f"#{keyword.replace(' ', '_').lower()}"
                    note1['proposed_tags'].add(tag_name)
                    note2['proposed_tags'].add(tag_name)
            else:
                print(f"    Not enough shared keywords ({len(shared_keywords)}) for connection (required: {MIN_SHARED_KEYWORDS_FOR_CONNECTION}).")

    print(f"Total connections found: {connections_found_count}")
    if connections_found_count == 0:
        print("  No connections were found based on the current MIN_SHARED_KEYWORDS_FOR_CONNECTION. Consider lowering it if you expect connections.")
        print(f"  Current MIN_SHARED_KEYWORDS_FOR_CONNECTION: {MIN_SHARED_KEYWORDS_FOR_CONNECTION}")

    # Apply the proposed changes (backlinks and tags) to the actual note files
    print("\nApplying changes to notes...")
    files_updated_count = 0
    for note_id, note_data in note_map.items():
        filepath = note_data['filepath']
        backlinks = note_data['proposed_backlinks']
        tags = note_data['proposed_tags']

        print(f"  Processing file: {os.path.basename(filepath)}")
        print(f"    Proposed backlinks: {backlinks}")
        print(f"    Proposed tags: {tags}")

        if not backlinks and not tags:
            print(f"    No changes for {os.path.basename(filepath)}. Skipping.")
            continue # No changes for this note

        try:
            with open(filepath, 'r+', encoding='utf-8') as f:
                content = f.read()

                # --- Simple Heuristic to avoid duplicate entries on re-run ---
                # Remove lines previously generated by this script.
                # This assumes generated links/tags start with specific prefixes.
                content_lines = content.splitlines()
                new_content_lines = []
                for line in content_lines:
                    if not (line.strip().startswith("Links generated: ") or
                            line.strip().startswith("Tags generated: ")):\
                        new_content_lines.append(line)

                # Re-join content, remove trailing whitespace from previous pass
                updated_content = "\n".join(new_content_lines).strip()

                # Add new backlinks section at the end of the note
                if backlinks:
                    # Sort for consistent output
                    updated_content += "\n\n" + "Links generated: " + " ".join(sorted(list(backlinks)))

                # Add new tags section at the end of the note
                if tags:
                    # Sort for consistent output
                    updated_content += "\n" + "Tags generated: " + " ".join(sorted(list(tags)))

                # Write the updated content back to the file
                f.seek(0) # Go to the beginning of the file
                f.truncate() # Clear existing content
                f.write(updated_content)
            print(f"  Updated: {os.path.basename(filepath)} (Added {len(backlinks)} links, {len(tags)} tags)")
            files_updated_count += 1
        except Exception as e:
            print(f"  Error updating {filepath}: {e}")

    print(f"Connections and tags generation complete. Total files updated: {files_updated_count}.")

def main():
    """Main function to orchestrate the Obsidian linking process."""
    vault_directory = input("Enter the path to your Obsidian vault directory: ").strip()

    if not os.path.isdir(vault_directory):
        print(f"Error: Directory not found at '{vault_directory}'")
        return

    setup_database()
    process_vault(vault_directory)
    create_connections_and_tags()
    print("\nObsidian linking process finished. Please check your notes.")

if __name__ == "__main__":
    main()

# MIN_SHARED_KEYWORDS_FOR_CONNECTION line 36
# NUM_KEYWORDS_PER_NOTE line 39
#  to change the behavior of the connection and tag generation process
