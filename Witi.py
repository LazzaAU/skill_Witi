from core.device.model.Device import Device
from core.base.model.AliceSkill import AliceSkill
from core.dialog.model.DialogSession import DialogSession
from core.util.Decorators import IntentHandler
from core.commons import constants
from core.user.model.AccessLevels import AccessLevel
from skills.Telegram import Telegram
import RPi.GPIO as GPIO
from pathlib import Path
import json


class Witi(AliceSkill):
	"""
	Author: lazza
	Description: Turn on and off your WITI alarm
	 - grab the intent " i'm leaving" etc
	 - Onleaving - turn the alarm on and send msg to telegram
	 - on return - if the user is known turn off the alarm, else ask for a pin number then send a telegram message
	 - ** If autoarming is enabled **
	 - If paring is disconnected, switch the alarm on.
	 - If re paired turn alarm off and send Telegram message.

	- If alarm triggers while away send a telegram message and play a sound if enabled

	"""
	DATABASE = {
		'witi': [
			'event TEXT NOT NULL UNIQUE',
			'active INTEGER NOT NULL DEFAULT 0',
		]
	}

	_ALARM_STATE = 20
	_TRIGGERED_STATE = 6
	_SWITCH_ALARM = 13
	_IGNITION_FEED = 19
	_PAIRED_TO_VEHICLE = 21


	def __init__(self):

		self._gpioPin = dict()
		self._alarmHasBeenTriggered = False
		self._presenceObject = dict()
		self._sessionID = ""
		# noinspection PyTypeChecker
		self._satelliteUID: Device = None
		self._autoArmingActive = False
		self._ignMessageSent = False
		self._previousMQTTMessage = dict()
		self._witiDatabaseValues = dict()
		self._voiceControlled = False
		self._homeassistantActive = False

		GPIO.setmode(GPIO.BCM)
		GPIO.setwarnings(False)
		GPIO.setup(Witi._ALARM_STATE, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
		GPIO.setup(Witi._TRIGGERED_STATE, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
		GPIO.setup(Witi._IGNITION_FEED, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
		GPIO.setup(Witi._PAIRED_TO_VEHICLE, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
		GPIO.setup(Witi._SWITCH_ALARM, GPIO.OUT)

		super().__init__(databaseSchema=self.DATABASE)


	ALARM_CODE = "alarm code"


	# When the Pi boots up, do the following
	def onBooted(self) -> bool:
		# run other onbooted code
		super().onBooted()

		# Read and Store database items in a object
		self.readDatabase()

		# If Witi database is empty, add default values
		if not self._witiDatabaseValues:
			self.logInfo('Doing Initial setup of the WITI DataBase')
			item = ['welcomeMessage', 'AlarmState', 'pinCode', 'mqttMessage', 'telegramID', 'telegramReminder']
			for rowItem in item:
				self.databaseInsert(
					tableName='witi',
					values={
						"event" : rowItem,
						"active": 0
					}
				)

		# reset the alarm to previous state in the event that WITI crashed and rebooted
		if self._witiDatabaseValues["AlarmState"] == 0 or not self._witiDatabaseValues["AlarmState"]:
			GPIO.output(Witi._SWITCH_ALARM, False)
			self.UserManager.home()
			self.updatePresenceDictionary(userchecking=False, userHome=True)
		else:
			GPIO.output(Witi._SWITCH_ALARM, True)
			self.UserManager.leftHome()
			self.updatePresenceDictionary(userchecking=False, userHome=False)

		# Get the UID of the satellite so we know where to speak to
		device = self.DeviceManager.getAliceTypeDevices(includeMain=False, connectedOnly=False)
		self._satelliteUID = device[0].name

		# Check the status and settings of telegram
		self.telegramStatusCheck()

		# Display welcome message and IP address if MQTT is enabled. (Triggers only once)
		if self.getConfig('enableMQTTmessages') and not self.getConfig('firstStartUp') and self._witiDatabaseValues[
			"telegramID"]:
			ipAddress = self.Commons.getLocalIp()
			text = f"Welcome to WITI voice control. To recieve MQTT messages for your further personalised configuration, then " \
				   f" please subscribe to the following MQTT broker on the topic 'WitiAlarm'. MQTT broker = {ipAddress}"
			self.sendTelegramMessage(text)

			# Update the config file to prevent seeing this message each start up
			self.updateConfig(key='firstStartUp', value='true')

		# delay reading GPIO pin states by 2 seconds
		self.ThreadManager.doLater(
			interval=2,
			func=self.stateMonitor
		)
		return True


	########### Intent Handlers (captures speech triggers) ###########

	@IntentHandler('SwitchWitiState')
	def determineRequestedState(self, session: DialogSession, **_kwargs):
		"""
		User has made a voice request....(created a dialog session)
		- If the ignition feed is on, then tell user that enabling the alarm can't be done
		- If the request was to turn "on" the alarm, enable the alarm
		- else disable the alarm

		"""
		if self.IgnitionFeedBack(session=session) and session.slotValue('WitiState') == 'on':
			return

		# we set _voiceControlled to True, it overrides automatic Arming/disarming. It will be True during any
		# intenthandler, as a intentHandler can't be triggered unless someone is at home and using their voice.
		self._voiceControlled = True

		# If user has requested the state of the alarm to be "on"....
		if session.slotValue('WitiState') == 'on':
			self.updatePresenceDictionary(userchecking=False, userHome=False)
			self.endSession(sessionId=session.sessionId)
			self.enableAlarm()
		else:
			self.disableAlarm(session=session)


	# If user has specifically stated to turn the alarm "on" then do this as theres no need to check
	# what state user is after
	@IntentHandler('WitiOnState')
	def requestedOnState(self, session: DialogSession, **_kwargs):
		if self.IgnitionFeedBack(session=session):
			return

		self._voiceControlled = True
		self.endSession(sessionId=session.sessionId)

		# set userState to away ('out')
		self.UserManager.leftHome()
		self.updatePresenceDictionary(userchecking=False, userHome=False)
		self.enableAlarm()


	@IntentHandler(intent='PinCode', requiredState='renewingPinCode')
	def renewPincode(self, session: DialogSession):
		""" User is updating their pinCode
			- Listen only for digits
			- Accept only if pin is 4 digits long
			- Update config if the above is true
			- Else return
		"""

		if 'Number' in session.slots:
			pin = ''.join([str(int(x.value['value'])) for x in session.slotsAsObjects['Number']])

			if len(pin) != 4:
				self.endDialog(
					sessionId=session.sessionId,
					text=f'That pin number is not 4 digits, You\'ll have to ask me again sorry',
					siteId=str(self._satelliteUID)
				)
			else:
				self.updateConfig(key='pinCode', value=pin)
				self.endDialog(
					sessionId=session.sessionId,
					text=f'pin code has been updated to {[digit for digit in pin]}',
					siteId=str(self._satelliteUID)
				)
		else:
			self.continueDialog(
				sessionId=session.sessionId,
				text='Sorry but i expected just numbers, please try again.',
				intentFilter=['PinCode'],
				currentDialogState='renewingPinCode'
			)


	@IntentHandler(intent='PinCode', requiredState='ListenForPinCode')
	def confirmPinCode(self, session: DialogSession, **_kwargs):
		""" If user has enabled forcePinCode setting for disarming the alarm then do this """

		# If user provides the correct PinCode
		if session.slotValue('Number') == self.getConfig('pinCode'):
			# Continue to disable the Alarm
			self.disarmCode(session=session, sendTelegram=True, user=session.user)
		else:
			# if incorrect pinCode. Abort and user will have to try again. Send a telgram message
			self.endDialog(
				sessionId=session.sessionId,
				text='Sorry but you provided me the wrong pin code. Aborting',
				siteId=str(self._satelliteUID)
			)
			self.sendTelegramMessage(f'{session.user} just provided a incorrect pinCode')


	@IntentHandler(intent='AnswerYesOrNo', requiredState='askingToCancelAlarm')
	def yesOrNoResponce(self, session: DialogSession):
		""" Checking if a user is home. If there's a responce... cancel arming the alarm"""

		# If a user responds with a yes to the question asked...
		if self.Commons.isYes(session):
			self.updatePresenceDictionary(userchecking=False, userHome=True)
			self.UserManager.home()
			self._voiceControlled = True
			self.endDialog(
				sessionId=session.sessionId,
				text='ok, cancelled',
				siteId=str(self._satelliteUID)
			)
			self.sendTelegramMessage(f'Enabling the alarm was cancelled by someone at home')
		else:
			# set user states and let the dialog timeout. When it timesout it is assumed no one is
			# home so alarm will get enabled.
			self.updatePresenceDictionary(userchecking=False, userHome=False)
			self.UserManager.leftHome()


	@IntentHandler('WitiSettings')
	def adjustWitiSettings(self, session: DialogSession):
		""" Allows user to adjust WITI settings via voice """

		if 'WitiState' in session.slotsAsObjects:
			if session.slotValue('configSetting') == "auto arming":
				self.updateConfigFileSetting(session=session, key='turnOnAutoArming')

			elif session.slotValue('configSetting') == Witi.ALARM_CODE and self.UserManager.hasAccessLevel(session.user,
																										   AccessLevel.ADMIN):
				self.updateConfigFileSetting(session=session, key='forcePinCode')
				self.sendTelegramMessage(f'{session.user} just turned PinCode to {session.slotValue("WitiState")}')

			elif session.slotValue('configSetting') == Witi.ALARM_CODE and not self.UserManager.hasAccessLevel(
					session.user, AccessLevel.ADMIN):
				self.endDialog(
					sessionId=session.sessionId,
					text=f'Sorry but i don\'t know who you are. Please call me by my name and try again',
					siteId=str(self._satelliteUID)
				)
				self.sendTelegramMessage(
					f'{session.user} just failed to turn {session.slotValue("WitiState")} the pincode setting')

			elif session.slotValue('configSetting') == "trigger sound":
				self.updateConfigFileSetting(session=session, key='activateSoundOnTrigger')

			elif session.slotValue('configSetting') == "mqtt":
				self.updateConfigFileSetting(session=session, key='enableMQTTmessages')



		elif not 'WitiState' in session.slotsAsObjects:
			# If user is trying to modify PinCode state they must be reognised
			if session.slotValue('configSetting') == Witi.ALARM_CODE:
				if self.UserManager.hasAccessLevel(session.user, AccessLevel.ADMIN):
					self.continueDialog(
						sessionId=session.sessionId,
						text='Sure, What pin code do you want to use?. Remember, It must be numbers',
						intentFilter=['PinCode'],
						currentDialogState='renewingPinCode',
						probabilityThreshold=0.1
					)
				else:
					self.endDialog(
						sessionId=session.sessionId,
						text='Sorry but i don\'t recognise you. Please call me by my name',
						siteId=str(self._satelliteUID)
					)

			elif session.slotValue('notification') == 'enabled':
				self.continueDialog(
					sessionId=session.sessionId,
					text='Sure what message do you want to send for enabled notifications?',
					intentFilter=['UserRandomAnswer'],
					currentDialogState='changingEnabledNotificationMessage',
					probabilityThreshold=0.1
				)

			elif session.slotValue('notification') == "disabled":
				self.continueDialog(
					sessionId=session.sessionId,
					text='Sure what message do you want to send for disabled notifications?',
					intentFilter=['UserRandomAnswer'],
					currentDialogState='changingDisabledNotificationMessage',
					probabilityThreshold=0.1
				)

			elif session.slotValue('notification') == "triggered":
				self.continueDialog(
					sessionId=session.sessionId,
					text='Sure what message do you want to send for your triggered notification ?',
					intentFilter=['UserRandomAnswer'],
					currentDialogState='changingTriggeredNotificationMessage',
					probabilityThreshold=0.1
				)


	@IntentHandler(intent='UserRandomAnswer', requiredState='changingTriggeredNotificationMessage')
	@IntentHandler(intent='UserRandomAnswer', requiredState='changingEnabledNotificationMessage')
	@IntentHandler(intent='UserRandomAnswer', requiredState='changingDisabledNotificationMessage')
	def changingNotificationMessage(self, session: DialogSession):
		""" Intents for changing notification messages"""

		if 'changingTriggeredNotificationMessage' in session.currentState:
			self.updateConfig(key='triggeredMessage', value=session.payload['input'])

		elif 'changingEnabledNotificationMessage' in session.currentState:
			self.updateConfig(key='enabledNotification', value=session.payload['input'])

		elif 'changingdisabledNotificationMessage' in session.currentState:
			self.updateConfig(key='disabledNotification', value=session.payload['input'])

		self.endDialog(
			sessionId=session.sessionId,
			text=f'Just changed that message to {session.payload["input"]}',
			siteId=str(self._satelliteUID)
		)


	def updateConfigFileSetting(self, session, key: str):
		""" Method for updating config.json values from true to false and visa versa"""
		if 'on' in session.slotValue('WitiState'):
			self.updateConfig(key=key, value="true")

		elif 'off' in session.slotValue('WitiState'):
			self.updateConfig(key=key, value="false")

		self.endDialog(
			sessionId=session.sessionId,
			text=f'No Problem, That setting is now {session.slotValue("WitiState")}',
			siteId=str(self._satelliteUID)
		)


	def sendTelegramMessage(self, message: str):
		"""
		:param message: a string of the message to send

		- Creates a telegram instance if ChatID is configured
		- Sends the message to the telegram bot
		"""
		if self._witiDatabaseValues['telegramID']:
			telegram = Telegram.Telegram()
			telegram.sendMessage(chatId=self._witiDatabaseValues['telegramID'], message=message)


	### Enable the Alarm
	def enableAlarm(self):
		self.logDebug(f'***** ENABLE ALARM *****')

		# If these states are true, don't enable the alarm
		if self.dontEnableAlarmStates():
			return

		# Run this block of code if Alarm is able to be turned ON
		if self.gpioState('AlarmState') == 'off':
			# No longer checking if a user is home so set to False
			self._presenceObject['checkingForUser'] = False
			# Print a debug message and inform user alarm is now set to on
			self.logDebug(f'Turning "ON" the alarm')
			self.say(
				text='Ok turning the alarm on',
				siteId=str(self._satelliteUID)
			)
			# Switch the actual GPIO pin on to enable alarm
			GPIO.output(Witi._SWITCH_ALARM, True)
			self.updateValueInDB(event='AlarmState', newState=1)
			self.sendTelegramMessage(self.getConfig('enabledNotification'))
		else:
			self.say(
				text='The alarm was already on. No further change done.',
				siteId=str(self._satelliteUID)
			)
			self._presenceObject['checkingForUser'] = False
			self.logWarning(f'The Alarm was already "{self.gpioState("AlarmState")}"')


	def disableAlarm(self, session):
		"""
		Disables the alarm under these following circumstances
		1. User is a recognised admin or..
		2. User has provided a pin number to disable the alarm
		"""
		# If alarm is already off, then abort and tell user
		if self.gpioState('AlarmState') == 'off':
			self.announceNoAction(session=session, state=self.gpioState('AlarmState'))
			return

		if self.getConfig('forcePinCode'):
			self.continueDialog(
				sessionId=session.sessionId,
				text='Sure. However I\'ll need your pin code first please.',
				intentFilter=['PinCode'],
				currentDialogState='ListenForPinCode',
				probabilityThreshold=0.1
			)
			return

		# If user is in the database and has admin rights. Then turn off the alarm
		if not constants.UNKNOWN_USER in session.user and self.UserManager.hasAccessLevel(session.user,
																						  AccessLevel.ADMIN):
			self.disarmCode(session=session, sendTelegram=True, user=session.user)

		else:
			self.endDialog(
				sessionId=session.sessionId,
				text='Sorry but i don\'t recognise you. Please call me by my name and ask again',
				siteId=str(self._satelliteUID)
			)


	def announceAction(self, session, state: str):
		""" Announce the state of the alarm """
		self.endDialog(
			sessionId=session.sessionId,
			text=f'Ok, turning the alarm *{state}* now',
			siteId=str(self._satelliteUID)
		)


	def announceNoAction(self, session, state: str):
		""" Announce no action to be taken """
		self.endDialog(
			sessionId=session.sessionId,
			text=f'The Alarm is already *{state}*, No further action taken',
			siteId=str(self._satelliteUID)
		)


	def stateMonitor(self):
		"""
		This is the main loop, triggered by a timer.
		* Purpose:
		1. Update the GPIO values with current states
		2. Monitor trailer unit pairing. If enabled:
			- Turn on alarm if pairing is disconnected
			- Turn off alarm if pairing is re connected
		3. Checks for user presence and updates those states
		4. Sends MQTT payload if enabled.
		5 informs user via telegram on these following states (if enabled)
			- Alarm has been auto turned on
			- Alarm has been auto turned off
			- Alarm has been triggered
			- Can't activate alarm if Iginition signal present
		"""
		self.updateGPIOvalues()

		# Send text messages via Telegram if Alarm is triggered and it's the first time it's been seen
		if self.gpioState('AlarmState') == "on" and self.gpioState(
				'triggeredState') == "on" and not self._alarmHasBeenTriggered:
			# set this var to prevent repeat messages
			self._alarmHasBeenTriggered = True
			self.logInfo(f'** ALARM HAS BEEN TRIGGERED ** ')

			# Send a telegram message
			self.sendTelegramMessage(self.getConfig('triggeredMessage'))

			# If user has enabled sounds. Trigger some user defined speech. (novelty feature)
			if self.getConfig('activateSoundOnTrigger'):
				self.say(
					text='Uploading live camera footage to the cloud. Also alerting neighbourhood watch contacts',
					siteId=str(self._satelliteUID)
				)

		# If alarm is on and triggered responce has timed out and trigger var is still True...
		# Inform user the alarm has reset and gone back to monitoring mode
		if self.gpioState('AlarmState') == "on" and self.gpioState(
				'triggeredState') == "off" and self._alarmHasBeenTriggered:
			self.logDebug(f"Alarm trigger is now {self.gpioState('AlarmState')}. Going back to monitoring mode")
			self.sendTelegramMessage('Alarm has now stopped making noise, but is still active')

			self._alarmHasBeenTriggered = False

		# if we can't detect the vehicle unit, assume vehicle is away and arm the alarm
		# providing some one hasn't manually disabled the alarm because they are home
		if not self._voiceControlled:
			self.autoArming()
		elif self.resetAutoArming():
			self._voiceControlled = False

		# send MQTT message if enabled
		self.mqttBrokerMessage()

		# every x amount of seconds, recheck the states
		self.ThreadManager.doLater(
			interval=self.getConfig('secondsBetweenUpdates'),
			func=self.stateMonitor
		)


	def mqttBrokerMessage(self):
		"""
		If enabled in the settings....

		1. update GPIO values with current states
		2. Set the MQTT payload values to human friendly states
		3. publish the MQTT payload to the "WitiAlarm" topic
		"""
		self.updateGPIOvalues()

		if self.getConfig('enableMQTTmessages'):

			mqttPayload = {
				"gpioStates": {
					"AlarmState"     : self.gpioState('AlarmState'),
					"triggeredState" : self.gpioState('triggeredState'),
					"IgnitionActive" : self.gpioState('IgnitionActive'),
					"PairedToVehicle": self.gpioState('PairedToVehicle'),
					"UserPresence"   : {
						'checkingUserPresence': self._presenceObject['checkingForUser'],
						"userHome"            : self.UserManager.checkIfAllUser("home"),
						"userOut"             : self.UserManager.checkIfAllUser("out"),
						'activeAutoArming'    : self._autoArmingActive,
						'VoiceControlled'     : self._voiceControlled
					}
				}
			}
			# Only publish MQTT message if a state changes
			if not mqttPayload == self._previousMQTTMessage:
				self.publish('WitiAlarm', payload=mqttPayload)

				self._previousMQTTMessage = mqttPayload
				self.logDebug(f'* - * The WITI MQTT Payload * - * ')
				self.logDebug(f'{mqttPayload}')
				print("........")


	def onLeavingHome(self):
		"""
		Triggers when a users state changes to leaving home ("out")
		"""
		super().onLeavingHome()
		if not self.UserManager.checkIfAllUser('home'):
			# set userState to away ('out')
			self.UserManager.leftHome()
			self.updatePresenceDictionary(userchecking=False, userHome=False)
			self.enableAlarm()


	def onReturningHome(self):
		"""
		Triggers when a users state changes to "home"
		"""
		if self.UserManager.checkIfAllUser('out') and self.gpioState('AlarmState') == 'on':
			super().onReturningHome()
			self.updatePresenceDictionary(userchecking=False, userHome=True)
			self.say(
				text='Welcome back. Please call me by my name and say, "Turn off the alarm ',
				siteId=str(self._satelliteUID)
			)


	def onSessionStarted(self, session):
		"""
		Get the session id of the request to cancel the alarm
		"""
		super().onSessionStarted(session)
		if 'askingToCancelAlarm' in session.currentState:
			self._sessionID = session.sessionId


	def onSessionTimeout(self, session):
		"""
		if session times out, assume no one is at home and turn on the alarm
		"""
		super().onSessionTimeout(session)

		if session.sessionId == self._sessionID:
			self.logDebug('A Session TimeOut occured. Triggering the enabling of the alarm.')
			self._sessionID = ""
			self._autoArmingActive = True
			# set userState to away ('out')
			self.UserManager.leftHome()
			self.updatePresenceDictionary(userchecking=False, userHome=False)
			self.enableAlarm()


	##################################   Auto Arming code  ################################
	def autoArming(self):
		"""
		If vehicle was paired but now its not:
		1. ask if anyones home
		2. if session timesout, assume no ones home and turn on alarm
		3. If user responds with yes - cancel alarm
		"""
		# if auto arming is not enabled by the user. return
		if not self.getConfig('turnOnAutoArming'):
			return

		# If pairing is lost, start setting up the user states
		if self.gpioState('PairedToVehicle') == "Disconnected" and self._presenceObject['checkingForUser'] == False \
				and not self._voiceControlled:
			self.updatePresenceDictionary(userchecking=True, userHome=False)

			if self.gpioState('AlarmState') == 'on':
				return

			# Ask user for a responce. No responce assumes no ones home and alarm will be enabled
			# when the dialog session times out.
			self.ask(
				text='I\'ve detected the vehicle has left. Reply with "yes" if you\'d like me to cancel turning on the alarm',
				intentFilter=['AnswerYesOrNo'],
				currentDialogState='askingToCancelAlarm',
				siteId=str(self._satelliteUID)
			)

		# Check if vehicle is paired, if so trigger turning off the alarm automatically
		if self.gpioState('PairedToVehicle') == "Connected" and self.gpioState(
				'AlarmState') == 'on' and self._autoArmingActive and not self._presenceObject['userHome']:
			self.logInfo('I have auto detected that the vehicle has returned')
			self.logDebug('Setting user state to home')
			self.UserManager.home()
			self.updatePresenceDictionary(userchecking=False, userHome=self.UserManager.checkIfAllUser('home'))

			# Say a welcome home reminder after "secondsAfterReturningHome" seconds (configured in settings)
			self.ThreadManager.doLater(
				interval=self.getConfig('secondsAfterReturningHome'),
				func=self.welcomeHome
			)


	def welcomeHome(self):
		"""
		Used in conjunction with a timer for:

		1. Setting user as home
		2. Informing user to turn off the alarm
		"""
		if self._autoArmingActive:
			self._autoArmingActive = False
			self.say(
				text='Welcome home, please call me by my name and ask me to, "Turn off the alarm" ',
				siteId=str(self._satelliteUID)
			)


	# Todo remove the logwarnings below
	def updatePresenceDictionary(self, userchecking: bool, userHome: bool):
		"""
		PresenceDictionary stores the values of a users home/away status.
		It also stores "userchecking" which is used to determine if the current state of the code
		is trying to determine if a user is home.
		"""
		if self.getConfig('useHomeAssistantPersonDetection'):
			if self.homeassistantPresenceDetection():
				self.logWarning('presenceDictionary has determined somes home via HA')
				self._presenceObject = {
					"checkingForUser": False,
					"someonesHome"   : True,
					"userHome"       : self.UserManager.checkIfAllUser("home"),
					"userOut"        : self.UserManager.checkIfAllUser("out")
				}
			else:
				self.logWarning('presenceDictionary has determined no ones home via HA')
				self._presenceObject = {
					"checkingForUser": False,
					"someonesHome"   : False,
					"userHome"       : self.UserManager.checkIfAllUser("home"),
					"userOut"        : self.UserManager.checkIfAllUser("out")
				}
		else:
			self.logWarning('presenceDictionary was run without HA support')

			self._presenceObject = {
				"checkingForUser": userchecking,
				"someonesHome"   : userHome,
				"userHome"       : self.UserManager.checkIfAllUser("home"),
				"userOut"        : self.UserManager.checkIfAllUser("out")
			}


	def gpioState(self, gpioString: str) -> str:
		"""
		Return a human friendly state of the pins for clarity in MQTT message and code reading
		"""
		state = self._gpioPin[gpioString]
		# todo swap connected for disconnected and visa versa
		if gpioString == 'PairedToVehicle':
			if state == 1:
				return 'Disconnected'
			elif state == 0:
				return 'Connected'
		else:
			if state == 1:
				return 'on'
			elif state == 0:
				return 'off'
			else:
				return 'unknown'


	def updateGPIOvalues(self):
		"""
		Update the gpio values with current pin states
		"""
		if GPIO.input(Witi._ALARM_STATE) == 0:
			self._alarmHasBeenTriggered = False
		self._gpioPin = {
			"AlarmState"     : GPIO.input(Witi._ALARM_STATE),
			"triggeredState" : GPIO.input(Witi._TRIGGERED_STATE),
			"IgnitionActive" : GPIO.input(Witi._IGNITION_FEED),
			"PairedToVehicle": GPIO.input(Witi._PAIRED_TO_VEHICLE)
		}


	def IgnitionFeedBack(self, session) -> bool:
		""" If ignition is on, inform user alarm can't be enabled"""
		if self.gpioState('IgnitionActive') == 'on':
			self.endDialog(
				sessionId=session.sessionId,
				text="Sorry, I can't do that while the Ignition is turned on",
				siteId=str(self._satelliteUID)
			)
			return True
		else:
			return False


	# todo remove this next dev code
	def devDisableCode(self, session=None):
		if session:
			self.announceAction(session=session, state="off")
		self.updatePresenceDictionary(userchecking=False, userHome=True)
		print(f'dev disable called')
		GPIO.output(Witi._SWITCH_ALARM, False)
		self.updateValueInDB(event='AlarmState', newState=0)
		self.UserManager.home()
		if session:
			self.sendTelegramMessage(f'Alarm was turned off by {session.user} via devCode')
		else:
			self.sendTelegramMessage(f'Alarm was turned off by devCode')


	def checkPossibleTowingState(self) -> bool:
		""" Check for possible towing states"""
		if self.gpioState('PairedToVehicle') == 'Connected':
			if self.gpioState('IgnitionActive') == 'on':
				return True

			elif self.gpioState('IgnitionActive') == 'off':
				self._ignMessageSent = False
				return False


	def manuallyDisabled(self) -> bool:
		if self._autoArmingActive and self._voiceControlled:
			return True


	def disarmCode(self, session=None, sendTelegram: bool = None, user: str = None):
		""" If Alarm is being disarmed
			- Announce alarm is disabled
			- Set user presence values
			- Send telegram if enabled
		"""
		if session:
			self.announceAction(session=session, state="off")
		self.logWarning('** ALARM IS BEING DISABLED **')
		self.updatePresenceDictionary(userchecking=False, userHome=True)
		GPIO.output(Witi._SWITCH_ALARM, False)
		self.updateValueInDB(event='AlarmState', newState=0)
		self.UserManager.home()

		if sendTelegram:
			if user:
				self.sendTelegramMessage(f'{user} has just disabled the alarm')

			else:
				self.sendTelegramMessage(f'The alarm has just been turned off by a unknown person')

		if self._autoArmingActive and not self._voiceControlled:
			self._voiceControlled = True


	def dontEnableAlarmStates(self) -> bool:
		"""
		If the ignition is on and vehicle "connected" or someone is at home. Don't enable the alarm
		"""
		if self.checkPossibleTowingState() or self._presenceObject[
			'someonesHome'] == True and not self._voiceControlled:
			self.logWarning(f'Either the Ignition is on or someone is Home, so not enabling alarm ')
			self._ignMessageSent = True
			return True

		# Prevent re auto enabling the alarm if a user is home but the vehicle is away
		# Also prevent arming if ignition is on
		if self.manuallyDisabled() or self.gpioState("IgnitionActive") == 'on':
			return False


	def resetAutoArming(self):
		"""
		 If alarm was disabled while pairing was disconnected and vehicle is now connected again
		 Reset vars so that autoarming will enable next time vehicle disconnects
		"""
		if self._voiceControlled and self.gpioState('PairedToVehicle') == 'Connected' and self.gpioState(
				'AlarmState') == 'off':
			self._voiceControlled = False


	def updateValueInDB(self, event: str, newState: int):
		"""
		update a value in the DataBase
		:param event: welcomeMessage', 'AlarmState', 'pinCode', 'mqttMessage', telegramID, telegramReminder
		:param newState: integer, often either 1 or 0 depending on the event
		:return: nothing
		"""
		self.DatabaseManager.update(
			tableName='witi',
			callerName=self.name,
			values={
				'active': newState
			},
			row=('event', event)
		)
		self.readDatabase()


	def readDatabase(self):
		""" Read the database and store values in a dictionary"""
		tempDict = self.databaseFetch(
			tableName='witi',
			query='SELECT * FROM :__table__', method='all'
		)

		for item in tempDict:
			self._witiDatabaseValues[item["event"]] = item["active"]


	def telegramStatusCheck(self):
		"""
		Do pre checks for if a Telegram ID is configured. If not, say a reminder to set it up.
		Message will only happen once.
		If user has confgured a TelegramId, write that ID to the WITI database for easier retrival
		"""
		if not self._witiDatabaseValues['telegramID']:
			try:
				telegramDB = Telegram.Telegram()
				# noinspection SqlResolve
				userID = telegramDB.databaseFetch(
					tableName='users',
					query='SELECT userId FROM :__table__'
				)
				self.updateValueInDB(event='telegramID', newState=userID['userId'])

			except:
				pass

		# If Telegram ID is not entered in settings. Advise the user
		if not self._witiDatabaseValues['telegramID'] and self._witiDatabaseValues['telegramReminder'] == 0:
			self.logWarning(f'No user telegram ID configured in the database')
			self.say(
				text=f'To use Telegram. Please add your telegram ID number to the Telegram skill settings',
				siteId=str(self._satelliteUID)
			)
			self.updateValueInDB(event='telegramReminder', newState=1)
			return False


	# todo clean up the logwarnings in the below block
	def homeassistantPresenceDetection(self) -> bool:
		""" Are people at home ?
		true = Yes people are home
		false = No ones home at the moment
		"""
		haStates = Path(f'{str(Path.home())}/skills/HomeAssistant/currentStateOfDevices.json')
		if self.HomeAssistantLoaded():
			self.logWarning(f'haStates file is {haStates}')
			data = json.loads(haStates.read_text())
			booleanName = self.getConfig('homeAssistantBooleanName')
			if data[booleanName] == 'off':
				self.logWarning('no ones home according to Home Assistant')
				return False
			elif data[booleanName] == 'on':
				self.logWarning('People are home according to Home Assistant')
				return True


	def HomeAssistantLoaded(self) -> bool:
		haStates = f'{str(Path.home())}/skills/HomeAssistant/currentStateOfDevices.json'

		if Path(haStates).exists():
			return True
		else:
			self.logWarning(f'HomeAssistant not loaded, disabling this option')
			self.updateConfig('useHomeAssistantPersonDetection', False)
			return False
