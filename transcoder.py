# InVision Video Concatenator
# transcoder.py

from __future__ import print_function
from slackclient import SlackClient

import glob
import json
import os
import shutil
import requests
import boto3
import botocore
from botocore.client import ClientError
from requests_toolbelt.multipart import MultipartEncoder
import time
import datetime as dt
import psycopg2
import urlparse
import uuid
import magic

urlparse.uses_netloc.append("postgres")
url = urlparse.urlparse(os.environ["DATABASE_URL"])

sql_conn = psycopg2.connect(
	database=url.path[1:],
	user=url.username,
	password=url.password,
	host=url.hostname,
	port=url.port
)

class SlackInterfacer(object):
	ACCEPTABLE_FILE_TYPES = ['mp4', 'mov', 'mpg', 'webm']

	# Credentials
	SLACK_VERIFICATION_TOKEN = os.environ["SLACK_VERIFICATION_TOKEN"]
	SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
	SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]

	DOWNLOAD_TOKEN = 'Bearer ' + SLACK_BOT_TOKEN # used to download a video file

	slack_bot_client = SlackClient(SLACK_BOT_TOKEN)
	slack_app_client = SlackClient(SLACK_APP_TOKEN) # used to call the 'files.list' command

	def __init__(self, channel, unique_id):
		super(SlackInterfacer, self).__init__()
		self.BOT_ID = self.getBotID()
		self.channel = channel
		self.channel_name = self.name_of_channel(channel)
		self.unique_id = unique_id

		# Local directory for saving original videos files downloaded from Slack
		self.orig_dir = "demo_videos/{}/{}/originals/".format(channel, unique_id)

		 # Create local video storage dirs if they do not exist
		if not os.path.exists(self.orig_dir):
			os.makedirs(self.orig_dir)

	def getBotID(self):
		bot_name = 'videobot'

		# Get the bot's identifier from Slack. This is used to ignore file uploads by the bot
		api_call = self.slack_bot_client.api_call("users.list")
		if api_call.get('ok'):
			# retrieve all users so we can find our bot
			users = api_call.get('members')
			for user in users:
				if 'name' in user and user.get('name') == bot_name:
					bot_id = user.get('id')
					print("Bot ID for '" + user['name'] + "' is " + bot_id)
					return bot_id
		else:
			print("could not find bot user with the name " + bot_name)
			return False

	def name_of_channel(self, channel):
		api_call = self.slack_bot_client.api_call(
			'channels.info',
			channel=channel
		)
		if api_call.get('ok'):
			name = api_call['channel']['name']
			return name
		else:
			# This could be a private channel
			api_call = self.slack_bot_client.api_call(
				'groups.info',
				channel=channel
			)
			if api_call.get('ok'):
				name = api_call['group']['name']
				return name
			else:
				print(api_call['error'] + channel)

			print(api_call['error'] + channel)

	def download_slack_videos(self, searchStartTime, searchEndTime):
		# Send a slack request to get all the files shared during the last week
		# Use slack_app_client because bots do not have access to 'files.list'
		api_call = self.slack_app_client.api_call(
			'files.list',
			channel=self.channel,
			ts_from=searchStartTime,
			ts_to=searchEndTime
		)
		print("API Call. Channel={} ts_from={} ts_to={}".format(self.channel, searchStartTime, searchEndTime))

		if api_call.get('ok'):
			files = api_call.get('files')
			print("API Result: {}".format(api_call))
			print("Found {} files for channel {}".format(len(files), self.channel))

			videos = []
			unaccepted = []

			for f in files:
				# We're only interested in demos not uploaded by the videobot itself
				if f['user'] != self.BOT_ID:
					downloadedVideo = self.download_source_file(f)
					if downloadedVideo['accepted']:
						videos.append(downloadedVideo)
					else:
						unaccepted.append(downloadedVideo)

			fileData = {
				'saved_videos':videos,
				'unaccepted':unaccepted
			}
			return fileData
		else:
			print("API Result: {}".format(api_call))
			return "Yikes! There was an error requesting this batch of files: " + api_call.get('error')

	def download_source_file(self, slack_file_object):
		localName = "{}.{}".format(slack_file_object['created'], slack_file_object['name'].split('.')[-1]) # name the files as <timestamp>.<extension>
		filePath = self.orig_dir + localName

		# if accepted file type
		if slack_file_object["filetype"] in self.ACCEPTABLE_FILE_TYPES:
			if slack_file_object['is_external'] == False:
				# # if hosted on slack, send an HTTP GET request to Slack to download file from given URL
				url = slack_file_object['url_private']
				r = requests.get(url, headers={'Authorization': self.DOWNLOAD_TOKEN}, stream=True)
			elif slack_file_object['external_type'] == 'dropbox':
				# if hosted on dropbox, prepare URL from dropbox
				url = slack_file_object['url_private']
				url = url[:(len(url)-1)] + "1" # replace 'dl=0' with 'dl=1' so we have a download dropbox link
				r = requests.get(url, stream=True)

			# Download the file
			print("Downloading to {} from {}".format(filePath, url))
			with open(filePath, 'wb') as file:
				for chunk in r.iter_content(chunk_size=1024):
					if chunk: # filter out keep-alive new chunks
						file.write(chunk)

			# Check that what was downloaded is a video file
			# this prevents errors from downloading dropbox web content instead of a video file
			fileType = magic.from_file(filePath, mime=True)
			if 'video' in fileType:
				vidInfo = {
					'name': slack_file_object['name'],
					'timestamp': slack_file_object['created'],
					'localpath': filePath,
					'user': slack_file_object['user'],
					'url': url,
					'id': slack_file_object['id'],
					'accepted': True
				}
				return vidInfo

		# Base case: this file can't be concatenated
		# but take note of it so it can be shared at the end of the week
		vidInfo = {
			'name': slack_file_object['name'],
			'user': slack_file_object['user'],
			'id': slack_file_object['id'],
			'url': slack_file_object['url_private'],
			'accepted': False
		}
		# Delete the file so it isn't sent to AWS
		try:
			os.remove(filePath)
		except IOError as e:
			print("Exception {} while trying to remove file {}".format(e, filePath))
		except:
			print("Unexpected error!")

		return vidInfo

	# Sends a video file to a users's DMs
	def send_video_to_user(self, user, concatFilePath, originalMetadata):
		print("UPLOADING: " + concatFilePath)
		count = len(originalMetadata['saved_videos'])

		if count >= 1:
			# Upload the file
			uploadedFileID = self.upload_file_to_slack(concatFilePath, user)[0]
			if uploadedFileID:
				# If successful upload, add comment
				videos = originalMetadata['saved_videos']
				shareMessage = """I hope you got some :popcorn: ready because here are the videos you requested from <#{}|{}>!
				There are {} demos included. Original uploaders were""".format(self.channel, self.channel_name, len(videos))

				# Add usernames of everyone involved
				# Use a set to avoid duplicates
				usernames = set()
				for vid in videos:
					usernames.add(vid['user'])
				for u in usernames:
					shareMessage += " <@{}>".format(u)

				# Post the message
				r = self.slack_bot_client.api_call(
					'files.comments.add',
					comment=shareMessage,
					file=uploadedFileID)
			else:
				print("problem with uploading {}".format(concatFilePath))

	# Posts the appropriate message to the slack channel
	def post_video(self, concatFilePath, originalMetadata):
		print("POSTING: " + concatFilePath)

		videos = originalMetadata['saved_videos']
		count = len(videos)

		if count >= 2:
			# Upload the file
			result = self.upload_file_to_slack(concatFilePath, self.channel)
			uploadedFileID = result[0]
			uploadedFileLink = result[1]
			if uploadedFileID:
				# If successful, let everyone know
				shareMessage = "{} demos included in this video. Featuring uploads by".format(count)

				# Add usernames of everyone involved
				# Use a set to avoid duplicates
				usernames = set()
				for vid in videos:
					usernames.add(vid['user'])
				for u in usernames:
					shareMessage += " <@{}>".format(u)

				# Post the message to the channel
				r = self.slack_bot_client.api_call(
					'files.comments.add',
					comment=shareMessage,
					file=uploadedFileID)

				# Notify subscribers of the upload
				self.notify_subscribers(uploadedFileLink)
			else:
				print("problem with uploading {}".format(concatFilePath))
		elif count == 1:
			# Remind people of the one video for the week
			loneUploader = ""
			for vid in videos:
				loneUploader = vid['user']

			soloURL = vid['url']
			soloVidMessage = "<@{}> posted the only demo video this week!\n{}".format(loneUploader, soloURL)
			self.slack_bot_client.api_call("chat.postMessage", channel=self.channel, text=soloVidMessage)

			self.notify_subscribers(soloURL)
		else:
			noVidMessage = "There were no demo videos posted this week in <#{}|{}>. :speak_no_evil:".format(self.channel, self.channel_name)
			self.slack_bot_client.api_call("chat.postMessage", channel=self.channel, text=noVidMessage)

			self.notify_subscribers()
		# Provide links to files that weren't concatenated
		unaccepted = originalMetadata['unaccepted']
		if len(unaccepted) > 0:
			admitFaultMessage = "There were some files I couldn't concatenate, so I linked them below. :upside_down_face: \n"

			for file in unaccepted:
				admitFaultMessage += "<@{}> {}\n".format(file['user'], file['url'])

			self.slack_bot_client.api_call("chat.postMessage", channel=self.channel, text=admitFaultMessage)

	# Uses the Slack API to upload a concatenated video file to given channel
	# channel is an argument here because scheduled videos are sent to the original channel
	# while manual ones are sent to users
	def upload_file_to_slack(self, file, channel):
		# my_file = {'file' : (file, open(file, 'rb'), 'mp4')}

		# get the name of the origin channel
		uploadDate = time.strftime("%m.%d.%Y")
		sourceChannel = self.name_of_channel(self.channel)
		filename = "Concatenated Demos - #{} - {}.mp4".format(sourceChannel, uploadDate)

		payload = {
		  "filename": filename,
		  "token": self.SLACK_BOT_TOKEN,
		  "channels": channel,
		}

                m = MultipartEncoder({
                    fields: { 'file' : (file, open(file, 'rb'), 'mp4') }
                    })

                r = requests.post("https://slack.com/api/files.upload", params = payload, data = m, headers = {'Content-Type': m.content_type})
		if r.json()["ok"] == True:
			print("Successful upload of {}".format(file))

			# Concatenated file was uploaded. Delete video stored locally
			shutil.rmtree(self.orig_dir)
			os.makedirs(self.orig_dir)

			# Delete the concatenated file stored locally
			os.remove(file)

			id_and_link = [r.json()['file']['id'], r.json()['file']['url_private']]

			# return the id of the uploaded file so the bot can drop a comment on it
			return id_and_link
		else:
			print(r)
			return False

	def get_subscribers(self):
		# Open a cursor to perform Postgres database operations
		cur = sql_conn.cursor()
		table_name = "subs_{}".format(self.channel)
		cur.execute("CREATE TABLE IF NOT EXISTS {} (user_id TEXT);".format(table_name))
		cur.execute("SELECT * FROM {};".format(table_name))
		# The result comes back as tuples
		subs = []
		for tple in cur.fetchall():
			subs.append(tple[0])

		cur.close()
		return subs

	def notify_subscribers(self, url="null"):
		print("Notifying subscribers")
		subscribers = self.get_subscribers()
		print("subs are {}".format(subscribers))

		if url == "null":
			noVidMessage = "There were no demo videos posted this week in <#{}|{}>. :speak_no_evil:".format(self.channel, self.channel_name)
			for sub in subscribers:
				self.slack_bot_client.api_call("chat.postMessage", channel=sub, text=noVidMessage)
		else:
			notification = "I hope you got some :popcorn: ready because here are this week's videos from <#{}|{}>\n{}!".format(self.channel, self.channel_name, url)
			for sub in subscribers:
				self.slack_bot_client.api_call("chat.postMessage", channel=sub, text=notification)

#******************************************************************************#

class AWSConcatenatorError(Exception):
	pass
# The AWSConcatenator class is adapted from a Boto 3 sample application
# which can be found here: https://github.com/boto/boto3-sample
class AWSConcatenator(object):
	# The following policies are for the IAM role.
	basic_role_policy = {
		'Statement': [
			{
				'Principal': {
					'Service': ['elastictranscoder.amazonaws.com']
				},
				'Effect': 'Allow',
				'Action': ['sts:AssumeRole']
			},
		]
	}
	more_permissions_policy = {
		'Statement': [
			{
				'Effect':'Allow',
				'Action': [
					's3:ListBucket',
					's3:Put*',
					's3:Get*',
					's3:*MultipartUpload*'
				],
				'Resource': '*'
			},
		]
	}

	def __init__(self, channel, unique_id):
		super(AWSConcatenator, self).__init__()

		self.channel = channel
		self.unique_id = unique_id
		self.s3_destination_path = '{}/{}/'.format(self.channel, self.unique_id)

		self.AWS_ACCESS_KEY = os.environ['AWS_ACCESS_KEY']
		self.AWS_SECRET_KEY = os.environ['AWS_SECRET_KEY']

		# Local (filesystem) related.
		self.unconcatenated_directory = 'demo_videos/{}/{}/originals/'.format(channel, unique_id)
		self.concatenated_directory = 'demo_videos/{}/output/'.format(channel)
		self.existing_files = set()
		self.file_pattern = '*.mp4'

		# AWS related.
		self.files_on_aws = []
		self.concat_file_on_aws = ''

		self.in_bucket_name = 'videobotinput'
		self.out_bucket_name = 'videobotoutput'
		self.role_name = 'concat-user'
		self.topic_name = 'concat-complete'
		self.queue_name = 'concat'
		self.pipeline_name = 'concat-pipe'
		self.region_name = 'us-west-2'
		self.pipeline_id = None

		self.in_bucket = None
		self.out_bucket = None
		self.role = None

		# How often to look at S3 for a concatenated file
		self.poll_interval = 10 # seconds

		self.s3 = boto3.resource('s3',
			aws_access_key_id=self.AWS_ACCESS_KEY,
			aws_secret_access_key=self.AWS_SECRET_KEY,
		)
		self.iam = boto3.resource('iam',
			aws_access_key_id=self.AWS_ACCESS_KEY,
			aws_secret_access_key=self.AWS_SECRET_KEY,
		)
		self.sns = boto3.resource('sns', self.region_name,
			aws_access_key_id=self.AWS_ACCESS_KEY,
			aws_secret_access_key=self.AWS_SECRET_KEY,
		)
		self.sqs = boto3.resource('sqs', self.region_name,
			aws_access_key_id=self.AWS_ACCESS_KEY,
			aws_secret_access_key=self.AWS_SECRET_KEY,
		)
		self.transcoder = boto3.client('elastictranscoder', self.region_name,
			aws_access_key_id=self.AWS_ACCESS_KEY,
			aws_secret_access_key=self.AWS_SECRET_KEY,
		)

	def ensure_local_setup(self):

		# Ensures that the local directory setup is sane by making sure
		# that the directories exist and aren't the same
		if self.unconcatenated_directory == self.concatenated_directory:
			raise AWSConcatenatorError(
				"The unconverted & converted directories can not be the same."
			)

		if not os.path.exists(self.unconcatenated_directory):
			os.makedirs(self.unconcatenated_directory)
		if not os.path.exists(self.concatenated_directory):
			os.makedirs(self.concatenated_directory)

	def ensure_aws_setup(self):
		# Ensures that the AWS services are set up
		if self.bucket_exists(self.in_bucket_name):
			self.in_bucket = self.s3.Bucket(self.in_bucket_name)
		else:
			self.in_bucket = self.s3.create_bucket(
				Bucket=self.in_bucket_name)

		if self.bucket_exists(self.out_bucket_name):
			self.out_bucket = self.s3.Bucket(self.out_bucket_name)
		else:
			self.out_bucket = self.s3.create_bucket(
				Bucket=self.out_bucket_name)

		if self.iam_role_exists():
			self.role = self.iam.Role(self.role_name)
		else:
			self.role = self.setup_iam_role()

		self.pipeline_id = self.get_pipeline()

	def check_unconcatenated_local(self):
		temp = os.listdir(self.unconcatenated_directory)
		files = []
		for t in temp:
			files.append(self.unconcatenated_directory + t)
		return files

	def start_uploading(self, files_found):
		files = []
		for file_path in files_found:
			filename = self.upload_to_s3(file_path)
			files.append(filename)

		return files

	def download_concatvid(self, s3_file):
		# Download a file from the S3 output bucket to your hard drive.
		destination_path = os.path.join(
			self.concatenated_directory,
			os.path.basename(s3_file)
		)

		while True:
			try:
				print('Waiting to download result')
				body = self.out_bucket.Object(s3_file).get()['Body']
				with open(destination_path, 'wb') as dest:
					# Here we write the file in chunks to prevent
					# loading everything into memory at once.
					for chunk in iter(lambda: body.read(4096), b''):
						dest.write(chunk)

				print("Downloaded {0}".format(destination_path))
				return destination_path
			except:
				time.sleep(self.poll_interval)
				pass

	# The boto-specific methods.
	def bucket_exists(self, bucket_name):
		# Returns ``True`` if a bucket exists and you have access to
		# call ``HeadBucket`` on it, otherwise ``False``.
		try:
			self.s3.meta.client.head_bucket(Bucket=bucket_name)
			return True
		except ClientError:
			return False

	def iam_role_exists(self):
		# Returns ``True`` if an IAM role exists.
		try:
			self.iam.meta.client.get_role(
				RoleName=self.role_name)
			return True
		except ClientError:
			return None

	def setup_iam_role(self):
		role = self.iam.create_role(
			RoleName=self.role_name,
			AssumeRolePolicyDocument=json.dumps(self.basic_role_policy))
		self.iam.RolePolicy(self.role_name, 'more-permissions').put(
			PolicyDocument=json.dumps(self.more_permissions_policy))
		return role

	def get_pipeline(self):
		# Get or create a pipeline. When creating, it is configured
		# with the previously set up S3 buckets and IAM role.
		# Returns its ID.
		paginator = self.transcoder.get_paginator('list_pipelines')
		for page in paginator.paginate():
			for pipeline in page['Pipelines']:
				if pipeline['Name'] == self.pipeline_name:
					return pipeline['Id']

		response = self.transcoder.create_pipeline(
			Name=self.pipeline_name,
			InputBucket=self.in_bucket_name,
			OutputBucket=self.out_bucket_name,
			Role=self.role.arn,
		)

		return response['Pipeline']['Id']

	def upload_to_s3(self, file_path):
		# Upload a file to the S3 input file bucket.
		filename = os.path.basename(file_path)
		destination = self.s3_destination_path + filename
		with open(file_path, 'rb') as data:
			self.in_bucket.Object(destination).put(Body=data)
		print("Uploaded raw video {0}".format(destination))
		# return filename
		return destination

	def start_concat(self, files):
		if files:
			inputs = []

			now = dt.datetime.now().strftime("%B %d, %Y %I-%M%p")
			name = 'Concatenated Demos - {}.mp4'.format(now)
			outputkey = '{}/{}'.format(self.channel, name)
			print("output key: " + outputkey)
			for f in files:
				inputs.append({
					'Key': f,
					'FrameRate': 'auto',
					'Resolution': 'auto',
					'AspectRatio': 'auto',
					'Interlaced': 'auto',
					'Container': 'auto'
				})

			self.transcoder.create_job(
				PipelineId=self.pipeline_id,
				Inputs=inputs,
				Outputs=[{
					'Key': outputkey,
					'PresetId': '1502739903620-sszgrv' # custom preset with padding
				}]
			)
			print("Started concatenating {}".format(files))
			return outputkey
		else:
			return

	def download_from_s3(self, s3_file):
		# Download a file from the S3 output bucket to your hard drive.

		destination_path = os.path.join(
			self.concatenated_directory,
			os.path.basename(s3_file)
		)
		body = self.out_bucket.Object(s3_file).get()['Body']
		with open(destination_path, 'wb') as dest:
			# Here we write the file in chunks to prevent
			# loading everything into memory at once.
			for chunk in iter(lambda: body.read(4096), b''):
				dest.write(chunk)

		print("Downloaded {0}".format(destination_path))
		return destination_path

	def delete_from_s3(self, orig_files, concat_file):
		s3 = boto3.client('s3',
			aws_access_key_id=self.AWS_ACCESS_KEY,
			aws_secret_access_key=self.AWS_SECRET_KEY,
		)

		# Add original files that were concatenated for deletion
		toDelete = []
		for f in orig_files:
			toDelete.append({'Key': f})

		# Delete original files from AWS
		d = {'Objects': toDelete}
		s3.delete_objects(Bucket = 'videobotinput', Delete = d)
		# Delete concatenated file from AWS
		s3.delete_object(Bucket = 'videobotoutput', Key = concat_file)


#******************************************************************************#

# run_process is used in queues from eventhandler.py
def run_process(request):
	# Setup
	channel = request.get('channel')
	user = request.get('user')
	searchStartTime = request.get('start')
	searchEndTime = request.get('end')

	# Create a unique UUID for this job.
	# This is used for file management to
	# ensure that jobs never access each other's files
	JOB_ID = uuid.uuid1()

	# Download video files within range from slack
	slack = SlackInterfacer(channel, JOB_ID)
	channelFileData = slack.download_slack_videos(searchStartTime, searchEndTime)
	savedVideos = channelFileData['saved_videos']

	# Interface with AWS
	concatenator = AWSConcatenator(channel, JOB_ID)
	concatenator.ensure_local_setup()
	concatenator.ensure_aws_setup()

	# If videos were downloaded, upload them to AWS for processing
	files_found = concatenator.check_unconcatenated_local()
	count = len(files_found)
	print("Found {} file(s) to process.".format(count))

	# Only send videos to AWS if there is more than one
	key = False
	uploaded = False
	if count >= 1:
		uploaded = concatenator.start_uploading(files_found)
		key = concatenator.start_concat(uploaded)

	downloadLoc = ""
	if key:
		downloadLoc = concatenator.download_concatvid(key)

	if user:
		slack.send_video_to_user(user, downloadLoc, channelFileData)
	else:
		slack.post_video(downloadLoc, channelFileData)

	if key and uploaded:
		# Delete videos from aws
		concatenator.delete_from_s3(uploaded, key)
