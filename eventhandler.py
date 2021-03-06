# InVision Video Concatenator
# eventhandler.py

import os
import time
import random
import datetime as dt
import requests
import threading
import json
import schedule
from slackclient import SlackClient
from flask import Flask, request, jsonify
from rq import Queue
from worker import conn
from transcoder import run_process
import psycopg2
import urlparse

urlparse.uses_netloc.append("postgres")
url = urlparse.urlparse(os.environ["DATABASE_URL"])

sql_conn = psycopg2.connect(
	database=url.path[1:],
	user=url.username,
	password=url.password,
	host=url.hostname,
	port=url.port
)

BOT_ID = ""

q = Queue(connection=conn)

ACCEPTABLE_FILE_TYPES = ['mp4', 'mov', 'mpg', 'webm']
emojiReactions = ['thumbsup', 'raised_hands', 'clap', 'ok_hand']

# Credentials
SLACK_VERIFICATION_TOKEN = os.environ["SLACK_VERIFICATION_TOKEN"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
SLACK_POSTING_CHANNEL = os.environ["SLACK_POSTING_CHANNEL"]

slack_bot_client = SlackClient(SLACK_BOT_TOKEN)

invalid_verification_message = "Error: Invalid Slack Authentication."

app = Flask(__name__)

def get_botID():
	bot_name = 'videobot'

	# Get the bot's identifier from Slack. This is used to ignore file uploads by the bot
	api_call = slack_bot_client.api_call("users.list")
	if api_call.get('ok'):
	# retrieve all users so we can find our bot
		users = api_call.get('members')
		for user in users:
			if 'name' in user and user.get('name') == bot_name:
				ID = user.get('id')
				print("Bot ID for '" + user['name'] + "' is " + ID)
				return ID
	else:
		print("could not find bot user with the name " + bot_name)
		return False

def add_deleted_file(channel, file_identifier):
    cur = sql_conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS deleted_files(channel_id TEXT, file_url TEXT);")
    cur.execute("INSERT INTO deleted_files VALUES (%s, %s);", [channel, file_identifier])
    sql_conn.commit()
    cur.close()

def get_last_execution(channel):
    cur = sql_conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS concat_executions (channel_id TEXT, exec_date DATE);")
    cur.execute("SELECT MAX(exec_date) FROM concat_executions WHERE channel_id=%s;", [channel])
    row = cur.fetchone()
    cur.close()
    if row is not None:
        return row[0]
    return None

### Accessing scheduled channels with SQL
def get_scheduled_channels():
	# Open a cursor to perform Postgres database operations
	cur = sql_conn.cursor()
	cur.execute("CREATE TABLE IF NOT EXISTS sched_channels (channel_id TEXT);")
	cur.execute("SELECT * FROM sched_channels;")
	# The result comes back as tuples
	channel_list = []
	for tple in cur.fetchall():
		channel_list.append(tple[0])
	cur.close()
	return channel_list

def schedule_current_channels():
	channels = get_scheduled_channels()
	for ch in channels:
		print("Scheduling " + ch)
		schedule_weekly(ch)

def add_channel_to_schedule(newChannel):
	schedule_weekly(newChannel)

	# Open a cursor to perform Postgres database operations
	cur = sql_conn.cursor()
	cur.execute("CREATE TABLE IF NOT EXISTS sched_channels (channel_id TEXT);")
	cur.execute("DELETE FROM sched_channels WHERE channel_id=%s;", [newChannel]) # Delete the value of newChannel if it's there
	cur.execute("INSERT INTO sched_channels VALUES (%s);", [newChannel]) # Add the value of newChanel to the table
	# Make the changes to the database persistent
	sql_conn.commit()
	cur.close()

def remove_channel_from_schedule(goodbyeChannel):
	# Open a cursor to perform Postgres database operations
	cur = sql_conn.cursor()
	cur.execute("CREATE TABLE IF NOT EXISTS sched_channels (channel_id TEXT);")
	cur.execute("DELETE FROM sched_channels WHERE channel_id=%s;", [goodbyeChannel])

	# Make the changes to the database persistent
	sql_conn.commit()
	cur.close()

	tag = '{}-weekly'.format(goodbyeChannel)
	schedule.clear(tag)



### Acessing Subscribers with SQL
def get_subscribers(channel):
	# Open a cursor to perform Postgres database operations
	cur = sql_conn.cursor()
	table_name = "subs_{}".format(channel)
	cur.execute("CREATE TABLE IF NOT EXISTS {} (user_id TEXT);".format(table_name))
	cur.execute("SELECT * FROM {};".format(table_name))
	# The result comes back as tuples
	subs = []
	for tple in cur.fetchall():
		subs.append(tple[0])

	cur.close()
	return subs

def subscribe(subscriber, channel):
	# Open a cursor to perform Postgres database operations
	cur = sql_conn.cursor()
	table_name = "subs_{}".format(channel)
	cur.execute("CREATE TABLE IF NOT EXISTS {} (user_id TEXT);".format(table_name))
	cur.execute("DELETE FROM {} WHERE user_id=%s;".format(table_name), [subscriber]) # Delete the value of subscriber if it's there
	cur.execute("INSERT INTO {} VALUES (%s);".format(table_name), [subscriber]) # Add the value of subscriber to the table

	# Make the changes to the database persistent
	sql_conn.commit()
	cur.close()

def unsubscribe(subscriber, channel):
	# Open a cursor to perform Postgres database operations
	cur = sql_conn.cursor()
	table_name = 'subs_{}'.format(channel)
	cur.execute("CREATE TABLE IF NOT EXISTS {} (user_id TEXT);".format(table_name))
	cur.execute("DELETE FROM {} WHERE user_id=%s;".format(table_name), [subscriber]) # Delete the value of subscriber if it's there

	# Make the changes to the database persistent
	sql_conn.commit()
	cur.close()



### Weekly Scheduling Functions
# Set the weekly job for a given channel
def schedule_weekly(channel):
	tag = '{}-weekly'.format(channel)
	schedule.clear(tag)
	# schedule.every(20).minutes.do(weekly_process, channel, SLACK_POSTING_CHANNEL).tag(tag)
	schedule.every().saturday.at("10:00").do(weekly_process, channel, SLACK_POSTING_CHANNEL).tag(tag) # Hour 10:00 UTC is 02:00AM PST

def weekly_process(channel, posting_channel):
    print("Weekly job running for channel {} and posting results in {} at time {}".format(channel, posting_channel, dt.datetime.now()))
    start_time = get_last_execution(channel)
    if start_time is None:
        now = int(time.time())
        start_time = now-604800 # 1 week ago
    else:
        print("Start time from DB: {}".format(start_time))
        start_time = time.mktime(start_time.timetuple())

    weeklyTask = {
        'channel':channel,
        'start': start_time,
        'end': time.time(),
        'posting_channel': posting_channel
    }
    print("Enqueuing job for scheduled job with payload: {}".format(weeklyTask))
    createQueue(weeklyTask)

# Runs an infinite loop to check if it's time to run a scheduled job
def schedule_loop():
	while True:
		schedule.run_pending()
		time.sleep(1)

### Flask App Functions
@app.route("/")
def index():
	return "Hey this is the Video Concatenator add me to your slack channel."

# @app.before_first_request
def starter():
	print("Running before first request")

	schedule_current_channels()

	global BOT_ID
	BOT_ID = get_botID()

	# Create a new thread to run the scheduling loop
	threading.Thread(target=schedule_loop).start()


@app.route("/run-schedule", methods=['GET'])
def weekly_process_rest():
    channel = request.args.get('channel')
    posting_channel = request.args.get('target_channel')

    if posting_channel is None:
        posting_channel = SLACK_POSTING_CHANNEL

    print("Weekly job running for channel {} and target channel {} at time {}".format(channel, posting_channel, dt.datetime.now()))
    weekly_process(channel, posting_channel)
    return ""

@app.route("/slack/events", methods=['GET', 'POST'])
def parse_event():
	data = request.get_json()
	if data.get('type') == "url_verification":
		# Slack is trying to verify the app. Better give them what they want
		return data.get('challenge')

	if data.get('token') == SLACK_VERIFICATION_TOKEN:
		event = data.get('event')
		if event:
			print("event {}".format(event))
			channel = event.get('channel')
			print("This channel is " + str(channel))

			# Respond to Slack Events
			if event.get('type') == 'member_joined_channel' and event.get('user') == BOT_ID:
				# The bot joined a channel; post an introduction
				introduction = "Hello! I'll concatenate videos every Friday at 1 PM PT :clapper:"
				api_call = slack_bot_client.api_call(
					'chat.postMessage',
					channel=channel,
					text=introduction)

				# Schedule a weekly concatenation
				add_channel_to_schedule(channel)
			elif event.get('subtype') == 'file_share' and event.get('user') != BOT_ID:
				# A file is being uploaded, and it's not from the videobot itself
				file = event.get('file')
				filetype = file.get('filetype')

				if filetype in ACCEPTABLE_FILE_TYPES:
					reaction = random.choice(emojiReactions)
				else:
					reaction = 'confused'

				api_call = slack_bot_client.api_call(
					'reactions.add',
					name=reaction,
					file=file['id']
				)
				if api_call.get('ok'):
					print("Reaction :{}: added to {}".format(reaction, file['name']))
				else:
					print("Error while adding reaction :{}: to {}".format(reaction, file['name']))
			elif event.get('subtype') == 'message_deleted' and event.get('user') != BOT_ID:
				# A message has been deleted and need to check if it contained a file
				previous_message = event.get('previous_message')
				if previous_message:
					deleted_file = previous_message.get('file')
					if deleted_file:
						file_identifier = deleted_file.get('url_private_download')
						add_deleted_file(channel, file_identifier)
	else:
		print("Invalid token received from Slack")

	return "" # has to return something or slack will error

def print_request(request):
	print("request: {}".format(request))

# This function enqueues a video concat request to be handled by the worker
def createQueue(request):
	channel = request.get('channel')

	# Throw the entire concatenation process in a background queue so as not to interrupt the webserver
	print("Enqueuing call for {}".format(channel))
	q.enqueue_call(
		func=run_process,
		args=(request,),
		timeout='30m')	# If it takes more than half an hour to concatenate some videos,
						# it's either a lost cause or way too many videos

# Respond to a '/concat' command
@app.route("/commands/concat", methods=['POST'])
def concat_slash_command():
	data = request.values
	if data.get('token') == SLACK_VERIFICATION_TOKEN:

		channel = data.get('channel_id')
		posting_channel = SLACK_POSTING_CHANNEL
		text = data.get('text')
		user = data.get('user_id')

		now = dt.datetime.now()
		nowString = now.strftime('%m-%d-%Y')

		dates = text.split()

		try:
			start = dt.datetime.strptime(dates[0], "%m-%d-%Y")
			end = dt.datetime.strptime(dates[1], "%m-%d-%Y")
		except:
			# Sample date sent as an example to user
			oneWeekAgo = dt.datetime.today() - dt.timedelta(days=7)
			oneWeekAgoString = oneWeekAgo.strftime('%m-%d-%Y')
			return "Please provide two dates in the format `mm-dd-yyyy` Example: `/concat {} {}`".format(oneWeekAgoString, nowString)

		# Set end to the beginning of the next day so it is an inclusive date range
		end = end + dt.timedelta(days=1)

		# Edge cases
		threeWeeks = dt.timedelta(weeks=3)
		if (end-start).total_seconds() > threeWeeks.total_seconds():
			return "Maximum date range is three weeks"
		elif (end-start).total_seconds() < 0:
			return "That date range doesn't make sense. :confused:"

		# Get Unix timestamps to send to Slack
		startUnix = time.mktime(start.timetuple())
		endUnix = time.mktime(end.timetuple())
		concatRequest = {
			'channel':channel,
			'posting_channel': posting_channel,
			'start':startUnix,
			'end':endUnix,
			'user':user,
			'manual_request':True
		}
		print("Enqueuing job for /concat with payload: {}".format(concatRequest))
		createQueue(concatRequest)

		return "I'll concatenate videos posted between *{}* and *{}* and will DM you when I'm done.\nThis can take several minutes.".format(dates[0], dates[1])
	else:
		return invalid_verification_message

# Respond to a '/schedule' command
@app.route("/commands/schedule", methods=['POST'])
def schedule_slash_command():
	data = request.values
	if data.get('token') == SLACK_VERIFICATION_TOKEN:
		channel = data.get('channel_id')
		user = data.get('user_id')

		if channel not in get_scheduled_channels():
			add_channel_to_schedule(channel)
			return "Great! I'll start concatenating videos every Friday at 1 PM PT. :smile:"
		else:
			return "I'm already concatenating videos every Friday at 1 PM PT. :relaxed:"
	else:
		return invalid_verification_message

# Respond to a '/cancel' command
@app.route("/commands/disable", methods=['POST'])
def disable_slash_command():
	data = request.values
	if data.get('token') == SLACK_VERIFICATION_TOKEN:
		channel = data.get('channel_id')
		user = data.get('user_id')

		if channel in get_scheduled_channels():
			remove_channel_from_schedule(channel)

			disable_message = "Weekly concatenation was turned *off* by <@{}> . You can re-enable it with `/schedule`".format(user)
			api_call = slack_bot_client.api_call(
				'chat.postMessage',
				channel=channel,
				text=disable_message)

			return "You disabled weekly concatenation for this channel."
		else:
			return "Weekly concatenation is already disabled for this channel. If you want to enable it, use `/schedule`."
	else:
		return invalid_verification_message

# Respond to a '/subscribe' command
@app.route("/commands/subscribe", methods=['POST'])
def subscribe_slash_command():
	data = request.values
	if data.get('token') == SLACK_VERIFICATION_TOKEN:
		channel = data.get('channel_id')
		user = data.get('user_id')

		subs = get_subscribers(channel)
		if user in subs:
			return "You are already subscribed to this channel. If you want to unsubscribe, use `/unsubscribe`"
		else:
			subscribe(user, channel)
			return "Awesome! I'll send you a direct message when I concatenate the videos on this channel. :smiley:"
	else:
		return invalid_verification_message

# Respond to a '/unsubscribe' command
@app.route("/commands/unsubscribe", methods=['POST'])
def unsubscribe_slash_command():
	data = request.values
	if data.get('token') == SLACK_VERIFICATION_TOKEN:
		channel = data.get('channel_id')
		user = data.get('user_id')

		subs = get_subscribers(channel)
		if user in subs:
			unsubscribe(user, channel)
			return "Ok, you will no longer receive messages when I post to this channel."
		else:
			return "You are not subscribed to this channel. If you want to subscribe, use `/subscribe`"
	else:
		return invalid_verification_message

# Respond to a '/list' command
@app.route("/commands/list", methods=['POST'])
def list_slash_command():
	data = request.values
	if data.get('token') == SLACK_VERIFICATION_TOKEN:
		channel = data.get('channel_id')

		allChannels = get_scheduled_channels()
		response = "The following channels are scheduled for weekly concatenation:\n"
		for ch in allChannels:
			channelName = name_of_channel(ch)
			response += " <#{}|{}>".format(ch, channelName)
		response += " and posting concatenated videos to  <#{}|{}>".format(SLACK_POSTING_CHANNEL, name_of_channel(SLACK_POSTING_CHANNEL))

		return response
	else:
		return invalid_verification_message

def name_of_channel(channel):
	api_call = slack_bot_client.api_call(
		'channels.info',
		channel=channel
	)
	if api_call.get('ok'):
		name = api_call['channel']['name']
		return name
	else:
		# This could be a private channel
		api_call = slack_bot_client.api_call(
			'groups.info',
			channel=channel
		)
		if api_call.get('ok'):
			name = api_call['group']['name']
			return name
		else:
			print(api_call['error'] + channel)

		print(api_call['error'] + channel)


@app.route("/commands/channeltest", methods=["POST"])
def demo_channel():
	data = request.values
	if data.get('token') == SLACK_VERIFICATION_TOKEN:
		channel = data.get('channel_id')
		posting_channel = data.get('target_channel')

		if posting_channel is None:
			posting_channel = SLACK_POSTING_CHANNEL

		weekly_process(channel, posting_channel)
	return "mkay"

# This doesn't run when on Heroku
if __name__ == "__main__":
	# Runs Flask on local environments for testing.
	app.run(port=4567)

if __name__ == 'eventhandler':
    starter()

