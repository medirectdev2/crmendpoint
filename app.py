from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
import psycopg2.extras
import os
import json
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
CORS(app)

DATABASE_URL = os.getenv('DATABASE_URL')
BEARER_TOKEN = os.getenv('BEARER_TOKEN')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_CLIENT_ID = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')

if not DATABASE_URL:
    raise RuntimeError('DATABASE_URL environment variable is not set')
if not BEARER_TOKEN:
    raise RuntimeError('BEARER_TOKEN environment variable is not set')
if not ZOHO_REFRESH_TOKEN or not ZOHO_CLIENT_ID or not ZOHO_CLIENT_SECRET:
    raise RuntimeError('Zoho credentials (ZOHO_REFRESH_TOKEN, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET) are not set')

zoho_access_token_cache = {
    'token': None,
    'expires_at': None
}

def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def convert_arrays_to_list(data):
    if isinstance(data, dict):
        return {k: convert_arrays_to_list(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [convert_arrays_to_list(item) for item in data]
    else:
        return data

def get_zoho_access_token():
    now = datetime.now()
    
    if zoho_access_token_cache['token'] and zoho_access_token_cache['expires_at']:
        if now < zoho_access_token_cache['expires_at']:
            return zoho_access_token_cache['token']
    
    token_url = "https://accounts.zoho.com/oauth/v2/token"
    params = {
        'refresh_token': ZOHO_REFRESH_TOKEN,
        'client_id': ZOHO_CLIENT_ID,
        'client_secret': ZOHO_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    
    response = requests.post(token_url, params=params)
    
    if response.status_code != 200:
        raise Exception(f'Failed to get Zoho access token: {response.text}')
    
    data = response.json()
    access_token = data.get('access_token')
    expires_in = data.get('expires_in', 3600)
    
    zoho_access_token_cache['token'] = access_token
    zoho_access_token_cache['expires_at'] = now + timedelta(seconds=expires_in - 60)
    
    return access_token

def fetch_from_zoho(module_name, record_id=None, criteria=None, fields=None):
    access_token = get_zoho_access_token()
    
    base_url = "https://www.zohoapis.com/crm/v2"
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}'
    }
    
    if record_id:
        url = f"{base_url}/{module_name}/{record_id}"
    elif criteria:
        # Use the /search endpoint when criteria is provided
        url = f"{base_url}/{module_name}/search"
    else:
        url = f"{base_url}/{module_name}"
    
    params = {}
    if criteria:
        params['criteria'] = criteria
    if fields:
        params['fields'] = fields
    
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code != 200:
        raise Exception(f'Failed to fetch from Zoho {module_name}: {response.text}')
    
    return response.json()

@app.route('/api/medical-experts-rec', methods=['POST'])
def get_medical_expert():
    auth_header = request.headers.get('Authorization')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Missing or invalid Authorization header'}), 401
    
    token = auth_header.split('Bearer ')[1]
    if token != BEARER_TOKEN:
        return jsonify({'error': 'Invalid token'}), 401
    
    aphra_number = request.args.get('aphra_number')
    if not aphra_number:
        return jsonify({'error': 'aphra_number parameter is required'}), 400
    
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute("""
            SELECT 
                aphra_number as "APHRA_Number",
                medical_expert_first_name as "Medical_Expert_First_Name",
                last_name as "Last_Name",
                doctor_id as "Doctor_ID",
                record_type as "Record_Type",
                record_id as "id"
            FROM medical_experts_rec 
            WHERE aphra_number = %s
        """, (aphra_number,))
        
        medical_expert = cursor.fetchone()
        
        if not medical_expert:
            return jsonify({'error': 'Medical expert not found'}), 404
        
        cursor.execute("""
            SELECT * FROM sectors_and_schemes 
            WHERE medical_expert = %s
        """, (medical_expert['id'],))
        
        sectors_and_schemes = cursor.fetchall()
        
        response = dict(medical_expert)
        
        formatted_sectors = []
        for sector in sectors_and_schemes:
            sector_dict = convert_arrays_to_list(dict(sector))
            formatted_sectors.append(sector_dict)
        
        response['Sectors_and_Schemes'] = formatted_sectors
        
        return jsonify(response), 200
        
    except psycopg2.Error as e:
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route('/api/medical-experts-zoho', methods=['POST'])
def get_medical_expert_from_zoho():
    auth_header = request.headers.get('Authorization')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Missing or invalid Authorization header'}), 401
    
    token = auth_header.split('Bearer ')[1]
    if token != BEARER_TOKEN:
        return jsonify({'error': 'Invalid token'}), 401
    
    aphra_number = request.args.get('aphra_number')
    if not aphra_number:
        return jsonify({'error': 'aphra_number parameter is required'}), 400
    
    try:
        medical_expert_fields = "id,Medical_Expert_First_Name,Last_Name,Doctor_ID,APHRA_Number,Vinici_User_Name"
        criteria = f"(APHRA_Number:equals:{aphra_number})"
        
        medical_expert_response = fetch_from_zoho(
            'Medical_Experts',
            criteria=criteria,
            fields=medical_expert_fields
        )
        
        if not medical_expert_response.get('data') or len(medical_expert_response['data']) == 0:
            return jsonify({'error': 'Medical expert not found'}), 404
        
        medical_expert = medical_expert_response['data'][0]
        medical_expert_id = medical_expert.get('id')
        
        sectors_criteria = f"(Medical_Expert:equals:{medical_expert_id})"
        sectors_response = fetch_from_zoho(
            'Sectors_and_Schemes',
            criteria=sectors_criteria
        )
        
        sectors_and_schemes = sectors_response.get('data', [])
        
        # Filter out Zoho system fields (those starting with $)
        cleaned_sectors = []
        for sector in sectors_and_schemes:
            cleaned_sector = {k: v for k, v in sector.items() if not k.startswith('$')}
            cleaned_sectors.append(cleaned_sector)
        
        response = {
            'APHRA_Number': medical_expert.get('APHRA_Number'),
            'Medical_Expert_First_Name': medical_expert.get('Medical_Expert_First_Name'),
            'Last_Name': medical_expert.get('Last_Name'),
            'Doctor_ID': medical_expert.get('Doctor_ID'),
            'Vinici_User_Name': medical_expert.get('Vinici_User_Name'),
            'id': medical_expert.get('id'),
            'Sectors_and_Schemes': cleaned_sectors
        }
        
        return jsonify(response), 200
        
    except Exception as e:
        return jsonify({'error': f'Zoho API error: {str(e)}'}), 500

@app.route('/api/zoho-modules', methods=['GET'])
def list_zoho_modules():
    auth_header = request.headers.get('Authorization')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Missing or invalid Authorization header'}), 401
    
    token = auth_header.split('Bearer ')[1]
    if token != BEARER_TOKEN:
        return jsonify({'error': 'Invalid token'}), 401
    
    try:
        access_token = get_zoho_access_token()
        
        url = "https://www.zohoapis.com/crm/v2/settings/modules"
        headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}'
        }
        
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            return jsonify({'error': f'Failed to fetch modules: {response.text}'}), 500
        
        modules_data = response.json()
        modules = modules_data.get('modules', [])
        
        module_list = [
            {
                'api_name': m.get('api_name'),
                'module_name': m.get('module_name'),
                'plural_label': m.get('plural_label'),
                'singular_label': m.get('singular_label')
            }
            for m in modules
        ]
        
        return jsonify({'modules': module_list}), 200
        
    except Exception as e:
        return jsonify({'error': f'Error: {str(e)}'}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'healthy'}), 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 3000))
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
