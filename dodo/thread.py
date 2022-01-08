#     Dodo - A graphical, hackable email client based on notmuch
#     Copyright (C) 2021 - Aleks Kissinger
#
# This file is part of Dodo
#
# Dodo is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Dodo is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Dodo. If not, see <https://www.gnu.org/licenses/>.

from PyQt5.QtCore import *
from PyQt5.QtWidgets import *
from PyQt5.QtGui import *
from PyQt5.QtWebEngineCore import *
from PyQt5.QtWebEngineWidgets import *

import subprocess
import json
import html
import email
import tempfile

from . import settings
from . import util
from . import keymap
from .panel import Panel


def flat_thread(d):
    "Return the thread as a flattened list of messages, sorted by date."

    thread = []
    def dfs(x):
        if isinstance(x, list):
            for y in x:
                dfs(y)
        else: thread.append(x)

    dfs(d)
    thread.sort(key=lambda m: m['timestamp'])
    return thread

def short_string(m):
    """Return a short string describing the provided message

    Currently, this just returns the contents of the "From" header, but something like a first name and
    short/relative date might be more useful.

    :param m: A JSON message object"""

    if 'headers' in m and 'From' in m['headers']:
        return m['headers']['From']

class MessageRequestInterceptor(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info: QWebEngineUrlRequestInfo):
        # print("intercepted")
        if settings.html_block_remote_requests:
            if not (info.resourceType() == QWebEngineUrlRequestInfo.ResourceTypeMainFrame or
                    info.requestUrl().toString()[0:4] == 'cid:'):
                info.block(True)


class EmbeddedImageHandler(QWebEngineUrlSchemeHandler):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.message = None

    def set_message(self, filename):
        with open(filename) as f:
            self.message = email.message_from_file(f)

    def requestStarted(self, request: QWebEngineUrlRequestJob):
        cid = request.requestUrl().toString()[4:]
        # print(f"got a request for content-id: {cid}")

        content_type = None
        if self.message:
            for part in self.message.walk():
                if "Content-id" in part and part["Content-id"] == f'<{cid}>':
                    print("found cid")
                    content_type = part.get_content_type()
                    buf = QBuffer(parent=self)
                    buf.open(QIODevice.WriteOnly)
                    buf.write(part.get_payload(decode=True))
                    buf.close()
                    request.reply(content_type.encode('latin1'), buf)
                    break

            # with open('/home/aleks/git/dodo/images/dodo-screen-inbox.png', 'rb') as f:
            #     buf.write(f.read())
            # buf.close()

        if not content_type:
            request.fail(QWebEngineUrlRequestJob.UrlNotFound)

class ThreadModel(QAbstractItemModel):
    """A model containing a thread, its messages, and some metadata

    This extends `QAbstractItemModel` to enable a tree view to give a summary of the messages, but also contains
    more data that the tree view doesn't care about (e.g. message bodies). Since this comes from calling
    "notmuch show --format=json", it contains information about attachments (e.g. filename), but not attachments
    themselves.

    :param thread_id: the unique thread identifier used by notmuch
    """

    def __init__(self, thread_id):
        super().__init__()
        self.thread_id = thread_id
        self.refresh()

    def refresh(self):
        """Refresh the model by calling "notmuch show"."""

        r = subprocess.run(['notmuch', 'show', '--format=json', '--include-html', self.thread_id],
                stdout=subprocess.PIPE, encoding='utf8')
        self.json_str = r.stdout
        self.d = json.loads(self.json_str)
        self.beginResetModel()
        self.thread = flat_thread(self.d)
        self.endResetModel()

    def message_at(self, i):
        """A JSON object describing the i-th message in the (flattened) thread"""

        return self.thread[i]

    def default_message(self):
        """Return the index of either the oldest unread message or the last message
        in the thread."""

        for i, m in enumerate(self.thread):
            if 'tags' in m and 'unread' in m['tags']:
                return i

        return self.num_messages() - 1

    def num_messages(self):
        """The number of messages in the thread"""

        return len(self.thread)

    def data(self, index, role):
        """Overrides `QAbstractItemModel.data` to populate a list view with short descriptions of
        messages in the thread.

        Currently, this just returns the message sender and makes it bold if the message is unread. Adding an
        emoji to show attachments would be good."""

        if index.row() >= len(self.thread):
            return None

        m = self.thread[index.row()]

        if role == Qt.DisplayRole:
            if 'headers' in m and 'From' in m["headers"]:
                return m['headers']['From']
            else:
                return '(message)'
        elif role == Qt.FontRole:
            font = QFont(settings.search_font, settings.search_font_size)
            if 'tags' in m and 'unread' in m['tags']:
                font.setBold(True)
            return font
        elif role == Qt.ForegroundRole:
            if 'tags' in m and 'unread' in m['tags']:
                return QColor(settings.theme['fg_subject_unread'])
            else:
                return QColor(settings.theme['fg'])

    def index(self, row, column, parent=QModelIndex()):
        """Construct a `QModelIndex` for the given row and (irrelevant) column"""

        if not self.hasIndex(row, column, parent): return QModelIndex()
        else: return self.createIndex(row, column, None)

    def columnCount(self, index):
        """Constant = 1"""

        return 1

    def rowCount(self, index=QModelIndex()):
        """The number of rows

        This is essentially an alias for :func:`num_messages`, but it also returns 0 if an index is
        given to tell Qt not to add any child items."""
        
        if not index or not index.isValid(): return self.num_messages()
        else: return 0

    def parent(self, index):
        """Always return an invalid index, since there are no nested indices"""

        return QModelIndex()


class ThreadPanel(Panel):
    """A panel showing an email thread

    This is the panel used for email viewing.

    :param app: the unique instance of the :class:`~dodo.app.Dodo` app class
    :param thread_id: the unique ID notmuch uses to identify this thread
    """

    def __init__(self, app, thread_id, parent=None):
        super().__init__(app, parent)
        window_settings = QSettings("dodo", "dodo")
        self.set_keymap(keymap.thread_keymap)
        self.model = ThreadModel(thread_id)
        self.thread_id = thread_id
        self.html_mode = settings.default_to_html

        self.subject = '(no subject)'
        self.current_message = -1

        self.splitter = QSplitter(Qt.Vertical)
        info_area = QWidget()
        info_area.setLayout(QHBoxLayout())

        self.thread_list = QListView()
        self.thread_list.setFocusPolicy(Qt.NoFocus)
        self.thread_list.setModel(self.model)
        self.thread_list.setFixedWidth(250)
        self.thread_list.clicked.connect(lambda ix: self.show_message(ix.row()))

        self.message_info = QTextBrowser()
        info_area.layout().addWidget(self.thread_list)
        info_area.layout().addWidget(self.message_info)
        self.splitter.addWidget(info_area)

        # TODO: this leaks memory, but stops Qt from cleaning up the profile too soon
        self.message_profile = QWebEngineProfile(self.app)

        self.image_handler = EmbeddedImageHandler(self)
        self.message_profile.installUrlSchemeHandler(b'cid', self.image_handler)
        self.message_request_interceptor = MessageRequestInterceptor(self.message_profile)
        self.message_profile.setUrlRequestInterceptor(self.message_request_interceptor)
        self.message_profile.settings().setAttribute(
                QWebEngineSettings.WebAttribute.JavascriptEnabled, False)

        self.message_view = QWebEngineView(self)

        # QWebEngineProfile.defaultProfile().setRequestInterceptor(self.message_request_interceptor)
        # self.message_view.settings().setAttribute(
        #         QWebEngineSettings.WebAttribute.JavascriptEnabled, False)

        page = QWebEnginePage(self.message_profile, self.message_view)
        self.message_view.setPage(page)

        self.message_view.setZoomFactor(1.2)
        self.splitter.addWidget(self.message_view)

        self.layout().addWidget(self.splitter)
        state = window_settings.value("thread_splitter_state")
        self.splitter.splitterMoved.connect(
                lambda x: window_settings.setValue("thread_splitter_state", self.splitter.saveState()))
        if state: self.splitter.restoreState(state)

        self.show_message(self.model.default_message())


    def title(self):
        """The tab title

        The title is given as the (shortened) subject of the currently visible message.
        """
        return util.chop_s(self.subject)

    def refresh(self):
        """Refresh the panel using the output of "notmuch show"

        Note the view of the message body is not refreshed, as this would pop the user back to
        the top of the message every time it happens. To refresh the current message body, use
        :func:`show_message` wihtout any arguments."""

        self.model.refresh()
        ix = self.thread_list.model().index(self.current_message, 0)
        if self.thread_list.model().checkIndex(ix):
            self.thread_list.setCurrentIndex(ix)

        m = self.model.message_at(self.current_message)

        if 'headers' in m and 'Subject' in m['headers']:
            self.subject = m['headers']['Subject']
        else:
            self.subject = '(no subject)'

        if 'headers' in m:
            header_html = ''
            header_html += f'<table style="background-color: {settings.theme["bg"]}; color: {settings.theme["fg"]}; font-family: {settings.search_font}; font-size: {settings.search_font_size}pt; width:100%">'
            for name in ['Subject', 'Date', 'From', 'To', 'Cc']:
                if name in m['headers']:
                    header_html += f"""<tr>
                      <td><b style="color: {settings.theme["fg_bright"]}">{name}:&nbsp;</b></td>
                      <td>{util.simple_escape(m["headers"][name])}</td>
                    </tr>"""
            if 'tags' in m:
                tags = ' '.join([settings.tag_icons[t] if t in settings.tag_icons else f'[{t}]' for t in m['tags']])
                header_html += f"""<tr>
                  <td><b style="color: {settings.theme["fg_bright"]}">Tags:&nbsp;</b></td>
                  <td><span style="color: {settings.theme["fg_tags"]}">{tags}</span></td>
                </tr>"""

            attachments = [f'[{part["filename"]}]' for part in util.message_parts(m)
                    if part.get('content-disposition') == 'attachment' and 'filename' in part]

            if len(attachments) != 0:
                header_html += f"""<tr>
                  <td><b style="color: {settings.theme["fg_bright"]}">Attachments:&nbsp;</b></td>
                  <td><span style="color: {settings.theme["fg_tags"]}">{' '.join(attachments)}</span></td>
                </tr>"""

            header_html += '</table>'
            self.message_info.setHtml(header_html)

        super().refresh()

    def show_message(self, i=-1):
        """Show a message

        If an index is provided, switch the current message to that index, otherwise refresh
        the view of the current message.
        """
        if i != -1: self.current_message = i

        if self.current_message >= 0 and self.current_message < self.model.num_messages():
            self.refresh()
            m = self.model.message_at(self.current_message)

            if 'unread' in m['tags']:
                self.tag_message('-unread')

            if self.html_mode:
                if 'filename' in m and len(m['filename']) != 0:
                    self.image_handler.set_message(m['filename'][0])

                html = util.body_html(m)
                if html: self.message_view.page().setHtml(html)
            else:
                text = util.colorize_text(util.simple_escape(util.body_text(m)))

                if text:
                    self.message_view.page().setHtml(f"""
                    <html>
                    <head>
                    <style type="text/css">
                    {util.make_message_css()}
                    </style>
                    </head>
                    <body>
                    <pre style="white-space: pre-wrap">{text}</pre>
                    </body>
                    </html>""")


    def next_message(self):
        """Show the next message in the thread"""

        self.show_message(min(self.current_message + 1, self.model.num_messages() - 1))

    def previous_message(self):
        """Show the previous message in the thread"""

        self.show_message(max(self.current_message - 1, 0))

    def scroll_message(self, lines=None, pages=None, pos=None):
        """Scroll the message body
        
        This operates in 3 different modes, depending on which arguments are given. Precisely one of the
        three arguments `lines`, `pages`, and `pos` should be provided.

        :param lines: scroll up/down the given number of 20-pixel increments. Negative numbers scroll up.
        :param pages: scroll up/down the given number of pages. Negative numbers scroll up.
        :param pos: scroll to the given position (possible values are 'top' and 'bottom')
        """
        if pos == 'top':
            self.message_view.page().runJavaScript(f'window.scrollTo(0, 0)',
                    QWebEngineScript.ApplicationWorld)
        elif pos == 'bottom':
            self.message_view.page().runJavaScript(f'window.scrollTo(0, document.body.scrollHeight)',
                    QWebEngineScript.ApplicationWorld)
        elif lines is not None:
            self.message_view.page().runJavaScript(f'window.scrollBy(0, {lines} * 20)',
                    QWebEngineScript.ApplicationWorld)
        elif pages is not None:
            self.message_view.page().runJavaScript(f'window.scrollBy(0, {pages} * 0.9 * window.innerHeight)',
                    QWebEngineScript.ApplicationWorld)

    def toggle_message_tag(self, tag):
        """Toggle the given tag on the current message"""

        m = self.model.message_at(self.current_message)
        if m:
            if tag in m['tags']:
                tag_expr = '-' + tag
            else:
                tag_expr = '+' + tag
            self.tag_message(tag_expr)

    def tag_message(self, tag_expr):
        """Apply the given tag expression to the current message

        A tag expression is a string consisting of one more statements of the form "+TAG"
        or "-TAG" to add or remove TAG, respectively, separated by whitespace."""

        m = self.model.message_at(self.current_message)
        if m:
            if not ('+' in tag_expr or '-' in tag_expr):
                tag_expr = '+' + tag_expr
            r = subprocess.run(['notmuch', 'tag'] + tag_expr.split() + ['--', 'id:' + m['id']],
                    stdout=subprocess.PIPE)
            self.app.invalidate_panels()
            self.refresh()

    def toggle_html(self):
        """Toggle between HTML and plain text message view"""

        self.html_mode = not self.html_mode
        self.show_message()

    def reply(self, to_all=True):
        """Open a :class:`~dodo.compose.ComposePanel` populated with a reply

        This uses the current message as the message to reply to. This should probably do something
        smarter if the current message is from the user (e.g. reply to the previous one instead).

        :param to_all: if True, do a reply to all instead (see `~dodo.compose.ComposePanel`)
        """

        self.app.compose(mode='replyall' if to_all else 'reply',
                         msg=self.model.message_at(self.current_message))

    def forward(self):
        """Open a :class:`~dodo.compose.ComposePanel` populated with a forwarded message
        """

        self.app.compose(mode='forward', msg=self.model.message_at(self.current_message))

    def open_attachments(self):
        """Write attachments out into temp directory and open with `settings.file_browser_command`

        Currently, this exports a new copy of the attachments every time it is called. Maybe it should
        do something smarter?
        """

        m = self.model.message_at(self.current_message)
        temp_dir, _ = util.write_attachments(m)
        
        if temp_dir:
            self.temp_dirs.append(temp_dir)
            cmd = settings.file_browser_command.format(dir=temp_dir)
            subprocess.Popen(cmd, shell=True)


