import os
import datetime
import yaml
import pathlib
import logging
import logging.config
import platform
from dataclasses import dataclass

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/drive"]


@dataclass
class DriveFolder:
    id: int
    name: str
    created_time: datetime.datetime

with open('logging.yaml', 'rt') as f:
    config = yaml.safe_load(f.read())
    
    def logging_dir():
        folder_name = 'drivebackup'
        try:
            if platform.system() == 'Windows':
                return os.path.join(os.environ['LOCALAPPDATA'], folder_name)
            else:
                return os.path.join('/', 'var', 'log', folder_name) 
            # sorry MAC
        except Error:
            print('ERROR - Unable to setup logging file')
    
    logging_dir = logging_dir()
    if logging_dir:
        os.makedirs(logging_dir, exist_ok=True)
        config['handlers']['file']['filename'] = os.path.join(logging_dir, 'drivebackup.log')

logging.config.dictConfig(config)
log = logging.getLogger('staging')


def main():
    try:
        service = build_service()

        conf = yaml.safe_load(pathlib.Path('config.yaml').read_text())

        backup_folder_id = find_or_create_folder(service, conf['backup folder'])
        root_folder = f'{platform.node()} {datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")}'
        root_folder_id = find_or_create_folder(service, root_folder, parent_id=backup_folder_id)

        for p in conf['paths to backup']:
            upload(service, p, parent_id=root_folder_id)

        existing_backups = ls(service, backup_folder_id)
        number_of_backups_to_keep = max(1, int(conf['backups to keep']) if 'backups to keep' in conf else len(existing_backups))
        
        number_of_backups_to_delete = len(existing_backups) - number_of_backups_to_keep
        if number_of_backups_to_delete > 0:
            existing_backups.sort(key=lambda x: x.created_time)
            backups_to_delete = existing_backups[0:number_of_backups_to_delete]
            for backup in backups_to_delete:
                log.warning('deleting old backup "%s" created at %s', backup.name, backup.created_time.strftime("%Y-%m-%d %H:%M:%S"))
                rm_r(service, backup.id)

    except HttpError as error:
        log.error("An error occurred %s", error)
    except TimeoutError as error:
        log.error("Timeout! %s", error)


def build_service():
  creds = None

  if os.path.exists("token.json"):
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
  # If there are no (valid) credentials available, let the user log in.
  if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
      creds.refresh(Request())
    else:
      flow = InstalledAppFlow.from_client_secrets_file(
          "credentials.json", SCOPES
      )
      creds = flow.run_local_server(port=0)
    # Save the credentials for the next run
    with open("token.json", "w") as token:
      token.write(creds.to_json())
  
  return build("drive", "v3", credentials=creds)


def upload_file(service, file_path, parent_id=None):
    log.info("uploading file %s", file_path)
    file_metadata = {'name': file_path.name}
    if parent_id:
        file_metadata['parents'] = [parent_id]
    try:
        media = MediaFileUpload(file_path, resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return file.get('id')
    except FileNotFoundError:
        log.error('file %s not found', file_path)


def find_or_create_folder(service, folder_name, parent_id=None):
    query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
    if parent_id:
        query = f"{query} and '{parent_id}' in parents"
    response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = response.get('files', [])
    
    if files:
        log.debug('folder "%s" exists', folder_name)
        return files[0]['id']
    else:
        log.info('folder "%s" does not exist, creating it...', folder_name)
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        if parent_id:
          file_metadata['parents'] = [parent_id]

        folder = service.files().create(body=file_metadata, fields='id').execute()
        return folder.get('id')
  

def create_path(service, path, parent_id=None):

    def inner(parts, parent_id):
        if len(parts) == 0:
            return parent_id
        folder, *remaining_parts = parts
        folder_id = find_or_create_folder(service, folder, parent_id)
        return inner(remaining_parts, folder_id) 

    return inner(pathlib.Path(path).parts, parent_id)


def upload(service, path, parent_id):
    log.debug("upload %s (%s)", path, parent_id)

    p = pathlib.Path(path)
    if not p.exists():
        log.error("%s does not exist", p)
    elif p.is_file():
        folder_id = create_path(service, p.parent, parent_id)
        upload_file(service, p, folder_id)
    else:
        root, dirs, files = next(os.walk(path))
        log.debug('root %s', root)
        log.debug('dirs %s', dirs)
        log.debug('files %s', files)
        folder_id = create_path(service, root, parent_id)

        for f in files:
            upload_file(service, pathlib.Path(os.path.join(root, f)), parent_id=folder_id)
        for d in dirs:
            upload(service, os.path.join(root, d), parent_id=folder_id)


def ls(service, parent_id='root'):
    drive_folders = []

    query = f"mimeType='application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed=false"
    response = service.files().list(q=query,
                                    spaces='drive',
                                    fields='files(id, name, createdTime)').execute()
    folders = response.get('files', [])

    for folder in folders:
        drive_folders.append(DriveFolder(
            id=folder['id'], 
            name=folder['name'], 
            created_time=datetime.datetime.fromisoformat(folder['createdTime'])
        ))


    return drive_folders


def rm_r(service, folder_id):
    service.files().delete(fileId=folder_id).execute()
    log.debug('removed folder with id %s', folder_id)


if __name__ == '__main__':
    main()
