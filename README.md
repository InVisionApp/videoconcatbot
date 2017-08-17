# InVision Slack Video Concatenator

The Video Concatenator is a bot for Slack that concatenates all the videos posted in a channel on a weekly basis. It also enables users to manually concatenate videos all the videos from a channel within a specified time range.

## Instructions for End Users
Anyone on the InVisionApp Slack team is able to access the Video Concatenator bot. Just invite __@videobot__ to your channel and it will be ready to take requests. Upon joining a channel, Video Concatenator schedules it to receieve a weekly concatenated video at 3 PM PT on Fridays. If you would like to disable automatic weekly videos, use `/disable`. The bot will post a message to the channel saying you have disabled weekly updates.

If you would like to get a direct message every time Video Concatenator finishes its weekly concatenation, go to the Slack channel of interest and type `/subscribe`. You can subscribe to as many channels as you like and will be notified of each one so long as it is scheduled. You can unsubscribe from a channel with `/unsubscribe`.

You can also ask Video Concatenator for a video containing all uploads within a specific date range. (The maximum is three weeks.) Use `/concat [mm-dd-yyyy] [mm-dd-yyyy]` and Video Concatenator will send you a direct message containing the uploads between the two dates given. The process can anywhere from two minutes to upwards of ten. Unfortunately, there is no current way to check the status of your concatenation.
### List of Video Concatenator Slack Commands
* `/schedule` -Reenable automatic weekly concatenation
* `/disable` -Disable automatic weekly concatenation. (**This affects everyone in the channel**)
* `/list` - Returns a list of all the channels scheduled for weekly concatenation.
* `/concat [startdate] [enddate]` -Get a file with a specific range for your own use.
* `/subscribe` -Get notified of weekly concatenated videos from this channel.
* `/unsubscribe` -Stop getting notified of weekly videos.

## Information for Developers

Application approved by security on 8/17/2016 by Jeremy Mount.

### Application Structure
The application is composed of three Python files,
* `eventhandler.py`
* `worker.py`
* `transcoder.py`

`eventhandler.py` is the web app. It listens for events and slash commands from Slack in the form of HTTP requests and acts on them. Any interaction with users that does involve moving a file occurs here. `eventhandler.py` also starts a thread that simply runs [schedule](https://schedule.readthedocs.io/en/stable/) to wait for Friday's weekly concatenation job.

`worker.py` runs in the background and waits to receive tasks from `eventhandler.py`.

`transcoder.py` does not run on its own, but contains all the code for handling files. When it's time to run a concatenation job, `eventhandler.py` adds the `run_process()` function from `transcoder.py` into `worker.py`'s queue

`transcoder.py` contains two classes: `SlackInterfacer` and `AWSConcatenator`. `SlackInterfacer` downloads source files from Slack channels and uploads concatenated files. `AWSConcatenator` 

## Running on Heroku
The folllowing Config Variables need to specified for authentication with Slack and AWS APIs.
```
$ heroku config:set AWS_ACCESS_KEY="your key"
$ heroku config:set AWS_SECRET_KEY="your key
$ heroku config:set SLACK_VERIFICATION_TOKEN="your token"
$ heroku config:set SLACK_BOT_TOKEN="your token"
$ heroku config:set SLACK_APP_TOKEN="your token"
```
A Postgres URL is also required, but is configured automatically when using the [Postgres add-on](https://www.heroku.com/postgres).

The [Redis To Go](https://elements.heroku.com/addons/redistogo) add-on for Heroku is required

The included `Procfile` specifies a web and worker dyno for Heroku.
```
web: gunicorn eventhandler:app
worker: python worker.py
```
`runtime.txt` contains one line to specify the Python version to use (`python-2.7.13`)
## Running Locally
Set environment variables
```
$ export AWS_ACCESS_KEY="your key"
$ export AWS_SECRET_KEY="your key
$ export SLACK_VERIFICATION_TOKEN="your token"
$ export SLACK_BOT_TOKEN="your token"
$ export SLACK_APP_TOKEN="your token"
$ export DATABASE_URL="your URL to postgres database"
```
Start a [redis](https://redis.io/topics/quickstart) server

__To install redis, do the following: (more information available [here](https://redis.io/topics/quickstart)):__
```
$ wget http://download.redis.io/redis-stable.tar.gz
$ tar xvzf redis-stable.tar.gz
$ cd redis-stable
$ make
```
__To run redis:__ `$ redis-server`

Create a virtual environment in the project directory.
```
$ virtualenv env
$ source env/bin/activate
$ pip install -r requirements.txt
$ python worker.py
```
In a new window within the project directory:
```
$ source env/bin/activate
$ python eventhandler.py
```
You can use [ngrok](https://ngrok.com/) to connect [Slack](https://api.slack.com/) to your localhost. The default port is 4567.

## General Technical Information
* Written in Python 2.7.13
* It's hosted on Heroku and video file storage and conversion is on AWS. Ownership of these resources can be transferred.
* The app requires the following Slack permissions: `bot`, `commands`, `files:read`, `users:read`. The app is managed through the Slack API portal and I can give access/ownership to whomever needs it.

## Dependencies
* [Slack Developer Kit for Python](https://github.com/slackapi/python-slackclient) for interfacing with Slack's API and interacting in Slack channels.
* [Requests](http://docs.python-requests.org/en/master/) The dev kit has built in support for sending HTTP requests to Slack, but Requests is used for more control during file downloads and uploads.
* [Flask](http://flask.pocoo.org/) to serve as the web app and handle incoming HTTP requests.
* [Gunicorn](http://gunicorn.org/) to be the server and make Flask work on Heroku.
* [Redis Queue](http://python-rq.org/) to enqueue background jobs to the worker program so the web app is responsive during lengthy video operations.
* [Redis](https://redis.io/) required for Redis Queue.
* [Schedule](https://schedule.readthedocs.io/en/stable/) for scheduling weekly operations.
* [Boto 3](https://github.com/boto/boto3-sample) for uploading and downloading to S3 and interfacing with AWS Elastic Transcoder.
* [python-magic](https://github.com/ahupp/python-magic) a python wrapper for libmagic, used to verify the filetype of downloads

### Relevant Slack API Reference

* [The Events API](https://api.slack.com/events-api)
	* [url_verification event](https://api.slack.com/events/url_verification)
	* [message event](https://api.slack.com/events/message)
	* [member_joined_channel event](https://api.slack.com/events/member_joined_channel)
	* [member_left_channel](https://api.slack.com/events/member_left_channel)

* Methods
  * [users.list](https://api.slack.com/methods/users.list)
  * [reactions.add](https://api.slack.com/methods/reactions.add)
  * [chat.postmessage](https://api.slack.com/methods/chat.postMessage)
  * [files.list](https://api.slack.com/methods/files.list)
  * [files.upload](https://api.slack.com/methods/files.upload)
  * [files.comments.add](https://api.slack.com/methods/files.comments.add)