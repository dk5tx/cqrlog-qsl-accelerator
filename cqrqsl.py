#!/usr/bin/env python3

"""
=============================================================================
CQRLOG QSL Accelerator
=============================================================================
Author: Ulf Schneider (DK5TX)
Date: 2026-10-23
Version: 1.1

Description: 
A fast, keyboard-driven terminal user interface (TUI) to process incoming 
QSL cards for CQRLOG. It directly updates the MariaDB/MySQL database.

Requirements:
- CQRLOG must be running in the background (to keep the DB port open).
- Modify 'DB_NAME' below to match your logbook number.
  By default, this script connects to Log No. 1 ('cqrlog001'). If you want to 
  process QSLs for a different log, change the DB_NAME variable below 
  (e.g., to 'cqrlog002' for Log No. 2).
=============================================================================
"""

import curses
import pymysql
import difflib
from datetime import datetime

# --- DATABASE CONFIGURATION ---
DB_HOST = '127.0.0.1'
DB_PORT = 64000
DB_USER = 'cqrlog'
DB_PASS = 'cqrlog'
DB_NAME = 'cqrlog001' 

def get_db_connection():
    """Establishes and returns a connection to the CQRLOG MariaDB/MySQL database."""
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor
    )

def search_qsos(callsign_query):
    """Searches the database for QSOs matching the callsign (with tolerance for typos) and sorts by relevance."""
    if not callsign_query:
        return []
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                SELECT id_cqrlog_main as id, callsign, qsodate, time_on, band, mode, 
                       qsl_r, qsl_s, qsl_via, qsls_date, qslr_date 
                FROM cqrlog_main 
                WHERE callsign LIKE %s 
                ORDER BY qsodate DESC, time_on DESC
            """
            search_pattern = f"%{callsign_query}%"
            cursor.execute(sql, (search_pattern,))
            results = cursor.fetchall()
            
            return sorted(
                results, 
                key=lambda x: difflib.SequenceMatcher(None, callsign_query.upper(), x['callsign']).ratio(), 
                reverse=True
            )
    finally:
        conn.close()

def update_qsl_status(qso_ids, status_type):
    """Updates the selected QSOs with the specified QSL received/sent flags and current date."""
    if not qso_ids:
        return
    
    conn = get_db_connection()
    current_date = datetime.now().strftime('%Y-%m-%d')
    
    try:
        with conn.cursor() as cursor:
            format_strings = ','.join(['%s'] * len(qso_ids))
            
            if status_type == 'R':
                # Mark as received only
                sql = f"UPDATE cqrlog_main SET qsl_r = 'Q', qslr_date = %s WHERE id_cqrlog_main IN ({format_strings})"
                params = [current_date] + qso_ids
                cursor.execute(sql, tuple(params))
                
            elif status_type == 'S':
                # Mark to send via bureau only (no received flag)
                sql = f"UPDATE cqrlog_main SET qsl_s = 'SB', qsls_date = %s WHERE id_cqrlog_main IN ({format_strings})"
                params = [current_date] + qso_ids
                cursor.execute(sql, tuple(params))

            elif status_type == 'BOTH':
                # Mark as received AND mark to send via bureau
                sql = f"UPDATE cqrlog_main SET qsl_r = 'Q', qslr_date = %s, qsl_s = 'SB', qsls_date = %s WHERE id_cqrlog_main IN ({format_strings})"
                params = [current_date, current_date] + qso_ids
                cursor.execute(sql, tuple(params))
                
        conn.commit()
    finally:
        conn.close()

def restore_qsl_status(qso_ids, original_states):
    """Restores the QSL flags and dates of selected QSOs to their exact state prior to the current session."""
    if not qso_ids:
        return
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            for qid in qso_ids:
                if qid in original_states:
                    orig = original_states[qid]
                    sql = """
                        UPDATE cqrlog_main 
                        SET qsl_r = %s, qsl_s = %s, qslr_date = %s, qsls_date = %s 
                        WHERE id_cqrlog_main = %s
                    """
                    cursor.execute(sql, (orig['qsl_r'], orig['qsl_s'], orig['qslr_date'], orig['qsls_date'], qid))
        conn.commit()
    finally:
        conn.close()

def get_confirmed_band_modes(qsos):
    """Parses a list of QSOs to identify which band/mode combinations have already been confirmed."""
    confirmed = set()
    for qso in qsos:
        r_status = str(qso['qsl_r']).upper()
        s_status = str(qso['qsl_s']).upper()
        
        if r_status in ('Y', 'Q') or s_status in ('Y', 'B', 'D', 'SB', 'SD', 'Q'):
            confirmed.add((qso['callsign'], qso['band'], qso['mode']))
    return confirmed

def main(stdscr):
    """Main application loop handling the terminal UI, user input, and state management."""
    curses.curs_set(1)
    stdscr.nodelay(False)
    curses.start_color()
    
    # Color Schemes
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)  # Selected row (Cursor)
    curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLUE)  # Multi-selection (Spacebar)
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK) # Search bar
    curses.init_pair(4, curses.COLOR_CYAN, curses.COLOR_BLACK)   # Table headers & lines

    search_mode = True
    search_query = ""
    qsos = []
    selected_indices = set()
    current_index = 0
    confirmed_combos = set()
    original_states = {}  # Stores initial state of QSOs for the Undo function

    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()

        # 1. Title bar / Shortcut hints
        header_title = " CQRLOG QSL Accelerator | Space: Select | R:RX | S:TX | B:Both | U:Undo | C:Search | ESC:Quit "
        stdscr.addstr(0, 0, header_title.ljust(width)[:width], curses.A_REVERSE)
        
        # 2. Table headers & separators
        sep_str = ("-" * (width - 1))[:width - 1]
        header_str = f" {'Callsign':<10} | {'Date        UTC':<17} | {'Band  Mode':<17} | {'RX   TX':<23} | VIA"
        header_str = header_str.ljust(width - 1)[:width - 1]

        stdscr.addstr(1, 0, sep_str, curses.color_pair(4))
        stdscr.addstr(2, 0, header_str, curses.color_pair(4) | curses.A_BOLD)
        stdscr.addstr(3, 0, sep_str, curses.color_pair(4))

        # 3. Draw QSO list
        header_offset = 4
        list_height = height - header_offset - 1
        
        for i in range(min(list_height, len(qsos))):
            qso = qsos[i]
            
            # Format Date and Time
            try:
                date_str = qso['qsodate'].strftime('%Y-%m-%d')
                time_str = str(qso['time_on'])[:5] 
            except:
                date_str = str(qso['qsodate'])
                time_str = str(qso['time_on'])
            datetime_part = f"{date_str}  {time_str}"

            # Format Sent Date
            sdate_str = ""
            if qso.get('qsls_date'):
                try:
                    sdate_str = qso['qsls_date'].strftime('%Y-%m-%d')
                except:
                    sdate_str = str(qso['qsls_date'])
            
            # Band / Mode + Checkmark for confirmed combos
            chk_str = "[✓]" if (qso['callsign'], qso['band'], qso['mode']) in confirmed_combos else ""
            bm_part = f"{qso['band']:<4}  {qso['mode']:<5}  {chk_str:<4}"
            
            # RX / TX Status
            r_val = qso['qsl_r'] or '-'
            s_val = qso['qsl_s'] or '-'
            r_part = f"R:{r_val}"
            s_part = f"S:{s_val}"
            sdate_part = f"({sdate_str})" if sdate_str else ""
            status_part = f"{r_part:<3}  {s_part:<4}  {sdate_part:<12}"
            
            # VIA Routing Info
            via_str = f"VIA: {qso['qsl_via']}" if qso.get('qsl_via') else ""
            
            # Assemble row string
            display_str = f" {qso['callsign']:<10} | {datetime_part:<17} | {bm_part:<17} | {status_part:<23} | {via_str}"
            display_str = display_str.ljust(width - 1)[:width - 1]

            row_y = i + header_offset
            if row_y >= height - 1:
                break

            if i == current_index and not search_mode:
                stdscr.addstr(row_y, 0, display_str, curses.color_pair(1))
            elif i in selected_indices:
                stdscr.addstr(row_y, 0, display_str, curses.color_pair(2))
            else:
                stdscr.addstr(row_y, 0, display_str)

        # 4. Search bar at the bottom
        stdscr.addstr(height - 1, 0, " Callsign Search: " + search_query, curses.color_pair(3))
        stdscr.refresh()

        key = stdscr.getch()

        if key == 27: # ESC
            break

        if search_mode:
            if key in (curses.KEY_ENTER, 10, 13):
                qsos = search_qsos(search_query)
                confirmed_combos = get_confirmed_band_modes(qsos)
                
                # Backup exact original state for Undo functionality
                for qso in qsos:
                    if qso['id'] not in original_states:
                        original_states[qso['id']] = {
                            'qsl_r': qso['qsl_r'],
                            'qsl_s': qso['qsl_s'],
                            'qslr_date': qso['qslr_date'],
                            'qsls_date': qso['qsls_date']
                        }
                        
                selected_indices.clear()
                current_index = 0
                search_mode = False
                curses.curs_set(0)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                search_query = search_query[:-1]
            elif 32 <= key <= 126:
                search_query += chr(key).upper()
        else:
            # Navigation
            if key == curses.KEY_UP:
                current_index = max(0, current_index - 1)
            elif key == curses.KEY_DOWN:
                current_index = min(len(qsos) - 1, current_index + 1)
            elif key == ord(' '): 
                if current_index in selected_indices:
                    selected_indices.remove(current_index)
                else:
                    selected_indices.add(current_index)
                current_index = min(len(qsos) - 1, current_index + 1)
            
            # R : Mark Received Only
            elif key in (ord('r'), ord('R')):
                ids_to_update = [qsos[i]['id'] for i in (selected_indices if selected_indices else [current_index])]
                update_qsl_status(ids_to_update, 'R')
                qsos = search_qsos(search_query)
                confirmed_combos = get_confirmed_band_modes(qsos)
                selected_indices.clear()
            
            # S : Mark Sent (via Bureau) Only
            elif key in (ord('s'), ord('S')):
                ids_to_update = [qsos[i]['id'] for i in (selected_indices if selected_indices else [current_index])]
                update_qsl_status(ids_to_update, 'S')
                qsos = search_qsos(search_query)
                confirmed_combos = get_confirmed_band_modes(qsos)
                selected_indices.clear()

            # B : Mark Both (Received & Sent)
            elif key in (ord('b'), ord('B')):
                ids_to_update = [qsos[i]['id'] for i in (selected_indices if selected_indices else [current_index])]
                update_qsl_status(ids_to_update, 'BOTH')
                qsos = search_qsos(search_query)
                confirmed_combos = get_confirmed_band_modes(qsos)
                selected_indices.clear()
                
            # U : Undo (Restore Original State)
            elif key in (ord('u'), ord('U')):
                ids_to_update = [qsos[i]['id'] for i in (selected_indices if selected_indices else [current_index])]
                restore_qsl_status(ids_to_update, original_states)
                qsos = search_qsos(search_query)
                confirmed_combos = get_confirmed_band_modes(qsos)
                selected_indices.clear()
            
            # C : Start New Search
            elif key in (ord('c'), ord('C')):
                search_mode = True
                search_query = ""
                original_states.clear() # Reset undo history for the new search context
                curses.curs_set(1)

if __name__ == "__main__":
    curses.wrapper(main)
