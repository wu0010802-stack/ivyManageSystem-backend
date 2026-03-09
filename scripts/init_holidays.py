
import sys
import os
from datetime import date
import holidays

# Add current directory to path so we can import models
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from models.database import get_session, Holiday, init_database
from sqlalchemy import text

def init_holidays(years=None):
    session = get_session()
    # Alter column if exists or drop table. Let's try to alter first or just drop for simplicity in this task.
    try:
        session.execute(text("DROP TABLE IF EXISTS holidays CASCADE"))
        session.commit()
        print("Dropped existing holidays table.")
    except Exception as e:
        session.rollback()
        print(f"Error dropping table: {e}")
    finally:
        session.close()

    init_database()
    
    if years is None:
        years = [date.today().year, date.today().year + 1]

    session = get_session()
    try:
        print(f"Initializing Taiwan holidays for years: {years}")
        # Use Chinese names
        tw_holidays = holidays.country_holidays('TW', years=years, language='zh_TW')

        count = 0
        for date_obj, name in tw_holidays.items():
            # Check if exists
            existing = session.query(Holiday).filter(Holiday.date == date_obj).first()
            if not existing:
                holiday = Holiday(
                    date=date_obj,
                    name=name,
                    is_active=True
                )
                session.add(holiday)
                count += 1
                print(f"Added holiday: {date_obj} - {name}")
            else:
                # Update name if changed?
                if existing.name != name:
                   existing.name = name
                   print(f"Updated holiday name: {date_obj} - {name}")
        
        session.commit()
        print(f"Successfully added {count} holidays.")
    except Exception as e:
        session.rollback()
        print(f"Error initializing holidays: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    init_holidays()
