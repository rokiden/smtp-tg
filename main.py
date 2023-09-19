import asyncio
import dataclasses
import email
import email.policy
import os
import signal
import traceback
from collections import deque
from datetime import datetime
from typing import cast

import aiogram
import aiogram.exceptions
import aiogram.utils.formatting as Format
from aiogram.types import BufferedInputFile
from aiosmtpd.controller import UnthreadedController
from aiosmtpd.smtp import Envelope


def log(text):
    print(datetime.now(), text)


@dataclasses.dataclass
class MailMsg:
    mail_from: str
    rcpt_tos: list[str]
    content: bytes
    id: int = None


class MailQueue:
    def __init__(self, folder):
        self.__folder = os.path.normpath(folder)
        self.__lock = asyncio.Lock()
        if not os.path.isdir(self.__folder):
            os.mkdir(self.__folder)
        self.__queue = deque(sorted(map(int, os.listdir(self.__folder))))
        self.__next_id = self.__queue[-1] + 1 if len(self.__queue) else 0
        if self.__queue:
            print('Queued items found: ', len(self.__queue))
        self.has_data = asyncio.Event()
        if self.__queue:
            self.has_data.set()

    def __id_path(self, id):
        return f'{self.__folder}{os.sep}{id}'

    async def push(self, msg: MailMsg):
        async with self.__lock:
            with open(self.__id_path(self.__next_id), 'wb') as f:
                f.writelines([(s + '\n').encode('utf8') for s in (
                    msg.mail_from,
                    ' '.join(msg.rcpt_tos)
                )])
                f.write(msg.content)
            self.__queue.append(self.__next_id)
            self.__next_id += 1
            self.has_data.set()

    async def pick(self):
        async with self.__lock:
            if self.__queue:
                id = self.__queue[0]
                with open(self.__id_path(id), 'rb') as f:
                    headers = [f.readline().decode('utf8').strip() for _ in range(2)]
                    mail_from = headers[0]
                    rcpt_tos = headers[1].split(' ')
                    content = f.read()
                    return MailMsg(mail_from, rcpt_tos, content, id)
            else:
                return None

    async def remove(self, msg: MailMsg):
        async with self.__lock:
            self.__queue.remove(msg.id)
            os.unlink(self.__id_path(msg.id))
            if not self.__queue:
                self.__next_id = 0
                self.has_data.clear()


class SMTPController(UnthreadedController):
    class Handler:
        def __init__(self, mqueue: MailQueue):
            self.__mqueue = mqueue

        async def handle_DATA(self, server, session, envelope: Envelope):
            await self.__mqueue.push(MailMsg(envelope.mail_from, envelope.rcpt_tos, envelope.original_content))
            return '250 OK'

    def __init__(self, mqueue: MailQueue, **kwargs):
        super().__init__(SMTPController.Handler(mqueue), **kwargs)

    async def begin(self):
        self.loop = asyncio.get_event_loop()
        self.server_coro = self._create_server()
        self.server = await self.server_coro

    async def end(self):
        await self.finalize()


class TGSender:
    def __init__(self, mqueue: MailQueue, token, default_chat, retry_period_min, retry_period_max, loop=None):
        self.__mqueue = mqueue
        self.__default_chat = default_chat
        self.__retry_period_min = retry_period_min
        self.__retry_period_max = retry_period_max
        self.__bot = aiogram.Bot(token)

        self.__loop = asyncio.get_event_loop() if loop is None else loop
        self.task: asyncio.Task | None = None

        self.__pending: MailMsg | None = None
        self.__pending_chats: list[int] | None = None
        self.__retry_period = self.__retry_period_min

    async def begin(self):
        await self.__bot.get_me()
        self.task = self.__loop.create_task(self.__serve_coro())

    async def end(self):
        tasks = []
        if self.task and not self.task.done():
            self.task.cancel()
            tasks.append(self.task)
        if self.__bot.session:
            tasks.append(self.__loop.create_task(self.__bot.session.close()))
        if tasks:
            await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)

    def __route(self, rcpt: str) -> int:
        if rcpt.endswith('@tg'):
            username = rcpt.split(sep='@')[0]
            if username.isnumeric():
                return int(username)

        return self.__default_chat

    def __route_multi(self, rcpt_tos: list[str]) -> deque[int]:
        return deque(set([self.__route(r) for r in rcpt_tos]))

    @staticmethod
    def __format(msg: MailMsg) -> tuple[Format.Text, BufferedInputFile | None]:
        emsg = cast(email.message.EmailMessage, email.message_from_bytes(msg.content, policy=email.policy.default))

        if 'From' in emsg:
            t_from = emsg['From']
            if msg.mail_from not in t_from:
                t_from = f'{msg.mail_from}/{t_from}'
        else:
            t_from = msg.mail_from

        t_to_msg = ','.join(msg.rcpt_tos)
        if 'To' in emsg:
            t_to = emsg['To']
            for rcpt in msg.rcpt_tos:
                if rcpt not in t_to:
                    t_to = f'{t_to_msg}/{t_to}'
                    break
        else:
            t_to = t_to_msg

        body_part = cast(email.message.EmailMessage, emsg.get_body(preferencelist=('related', 'plain')))
        t_body = Format.Underline('No content') if body_part is None else body_part.get_content().strip()

        text = (
                Format.as_line(Format.Italic(Format.Bold("From:")), ' ', t_from) +
                Format.as_line(Format.Italic(Format.Bold("To:")), ' ', t_to) +
                (Format.as_line(Format.Italic(Format.Bold("Subject:")), ' ',
                                emsg['Subject']) if 'Subject' in emsg else '') +
                Format.as_line() +
                Format.Text(t_body)
        )

        file_need = len(list(emsg.iter_parts())) > 1 or body_part is None

        if len(text) > 1024:
            text = text[:1024]
            file_need = True

        if file_need:
            file = BufferedInputFile(msg.content, filename="raw.txt")
        else:
            file = None
        return text, file

    async def __report_exc(self, exc: Exception, chat: int, msg_raw: bytes):
        try:
            text = (
                    Format.as_line(Format.Bold('SMTP-TG Exception:')) +
                    Format.Text(''.join(traceback.format_exception(exc))) +
                    Format.as_line(f'Chat: {chat}') +
                    Format.Text(msg_raw.decode('utf8'))
            )
            await self.__bot.send_message(chat_id=self.__default_chat, **text.as_kwargs())
        except:
            log('Exception')
            traceback.print_exception(exc)
            print('CHAT:', chat)
            print('RAW MSG:\n' + msg_raw.decode('utf8'))

    async def __consume_pending(self, chat):
        self.__pending_chats.remove(chat)
        if not self.__pending_chats:
            await self.__mqueue.remove(self.__pending)
            self.__pending = None

    async def __serve_coro(self):
        while True:
            if self.__pending is None:
                await self.__mqueue.has_data.wait()
                self.__pending = await self.__mqueue.pick()
                self.__pending_chats = self.__route_multi(self.__pending.rcpt_tos)
                self.__pending_format = self.__format(self.__pending)

            chat = self.__pending_chats[0]
            text, file = self.__pending_format

            try:
                await self.__bot.send_message(chat_id=chat, **text.as_kwargs())
                if file:
                    await self.__bot.send_document(chat_id=chat, document=file)
            except aiogram.exceptions.TelegramRetryAfter as e:
                log(e.message)
                await asyncio.sleep(e.retry_after + 1)
            except (aiogram.exceptions.TelegramNetworkError,
                    aiogram.exceptions.RestartingTelegram) as e:
                log(f'{type(e).__name__}: {str(e)}')
                await asyncio.sleep(self.__retry_period)
                self.__retry_period = min(self.__retry_period * 2, self.__retry_period_max)
            except aiogram.exceptions.AiogramError as e:
                msg_raw = self.__pending.content
                await self.__consume_pending(chat)
                self.__loop.create_task(self.__report_exc(e, chat, msg_raw))
            else:
                await self.__consume_pending(chat)
                self.__retry_period = self.__retry_period_min

            await asyncio.sleep(0)


async def main():
    # ENV
    smtp_host = os.getenv('SMTP_HOST', '0.0.0.0')
    smtp_port = int(os.getenv('SMTP_PORT', '2525'))
    mqueue_folder = os.getenv('MAIL_QUEUE_FOLDER', 'mail_queue')
    tg_token = os.getenv('TG_TOKEN')
    tg_default_chat = os.getenv('TG_DEFAULT_CHAT')
    tg_retry_period_min = os.getenv('TG_RETRY_PERIOD_MIN', str(30))
    tg_retry_period_max = os.getenv('TG_RETRY_PERIOD_MAX', str(5 * 60))
    stop_timeout = os.getenv('STOP_TIMEOUT', str(5))

    print('Mail queue folder:', mqueue_folder)
    print('SMTP host:', smtp_host)
    print('SMTP port:', smtp_port)
    print('TG default chat:', tg_default_chat)
    print('TG retry period min,max:', tg_retry_period_min, tg_retry_period_max)
    print('Stop timeout:', stop_timeout)

    assert tg_token is not None
    assert tg_default_chat is not None and tg_default_chat.isnumeric()

    tg_default_chat = int(tg_default_chat)
    tg_retry_period_min = int(tg_retry_period_min)
    tg_retry_period_max = int(tg_retry_period_max)
    stop_timeout = int(stop_timeout)

    # COMMON
    loop = asyncio.get_running_loop()
    evt_terminate = asyncio.Event()
    for signame in ('SIGINT', 'SIGTERM'):
        loop.add_signal_handler(getattr(signal, signame),
                                lambda *_: (log('Terminating'), evt_terminate.set()))

    mqueue = MailQueue(mqueue_folder)

    # SERVERS INIT AND RUN
    tg_sender = TGSender(mqueue, tg_token, tg_default_chat, tg_retry_period_min, tg_retry_period_max)
    await tg_sender.begin()

    smtp_ctrl = SMTPController(mqueue, hostname=smtp_host, port=smtp_port)
    await smtp_ctrl.begin()
    smtp_wait_closed_task = asyncio.create_task(smtp_ctrl.server.wait_closed())

    # WAITING RUNNING
    log('Started')
    done, pending = await asyncio.wait((
        tg_sender.task,
        smtp_wait_closed_task,
        asyncio.create_task(evt_terminate.wait())
    ), return_when=asyncio.FIRST_COMPLETED)
    for d in done:
        e = d.exception()
        if e is not None:
            traceback.print_exception(e)

    # WAITING FOR CLEANUP
    await asyncio.wait((
        asyncio.create_task(tg_sender.end()),
        asyncio.create_task(smtp_ctrl.end())
    ), return_when=asyncio.ALL_COMPLETED, timeout=stop_timeout)
    log('Finished')


if __name__ == '__main__':
    asyncio.run(main())
