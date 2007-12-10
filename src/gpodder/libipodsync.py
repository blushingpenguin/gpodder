# -*- coding: utf-8 -*-
#
# gPodder - A media aggregator and podcast client
# Copyright (C) 2005-2007 Thomas Perl <thp at perli.net>
#
# gPodder is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# gPodder is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

#
#  libipodsync.py -- sync localdb contents with ipod playlist
#  thomas perl <thp@perli.net>   20060405
#
#


# variable tells if ipod functions are to be enabled
enable_ipod_functions = True

# possible mp3 length detection mechanisms
MAD = 1
EYED3 = 2

# default length (our "educated guess") is 60 minutes
DEFAULT_LENGTH = 60*60*1000

# command line for mplayer
MPLAYER_COMMAND = 'mplayer -msglevel all=-1 -identify -vo null -ao null -frames 0 "%s" 2>/dev/null'

from gpodder import util

from liblogger import log

try:
    import gpod
except:
    log( '(ipodsync) Could not find python-gpod. iPod functions will be disabled.')
    log( '(ipodsync) Please install the "python-gpod" package if you want iPod support.')
    enable_ipod_functions = False

try:
    import mad
    log( '(ipodsync) Found pymad')
except:
    log( '(ipodsync) Could not find pymad.')

# are we going to use eyeD3 for cover art extraction?
get_coverart = False

try:
    import eyeD3
    get_coverart = True
    log( '(ipodsync) Found eyeD3, will try to extract cover art from mp3 metadata')
except:
    log( '(ipodsync) Could not find eyeD3 - falling back to channel cover for iPod cover art')

import os
import os.path
import glob
import shutil
import sys
import time
import string
import email.Utils

import libpodcasts
import libgpodder
import libconverter
import libtagupdate

import gobject

# do we provide iPod functions to the user?
def ipod_supported():
    global enable_ipod_functions
    return enable_ipod_functions

# file extensions that are handled as video
video_extensions = [ "mov", "mp4", "m4v", "divx" ]

# is mplayer available for finding track length?
use_mplayer = False
if util.find_command( 'mplayer') != None:
    use_mplayer = True
    log('(ipodsync) Found mplayer, using it to find track length of files')
else:
    log('(ipodsync) mplayer not found - length of video files will be guessed')

class gPodderSyncMethod:
    def __init__( self, callback_progress = None, callback_status = None, callback_done = None):
        self.callback_progress = callback_progress
        self.callback_status = callback_status
        self.callback_done = callback_done
        self.cancelled = False
        self.can_cancel = False
        self.errors = []
    
    def open( self):
        return False

    def set_progress( self, pos, max):
        if self.callback_progress:
            gobject.idle_add( self.callback_progress, pos, max)

    def set_progress_overall( self, pos, max):
        if self.callback_progress:
            gobject.idle_add( self.callback_progress, pos, max, True)

    def set_progress_sub_episode( self, pos, max):
        if self.callback_progress:
            gobject.idle_add( self.callback_progress, pos, max, False, True)

    def set_episode_status( self, episode):
        self.set_status( episode = _('Copying %s') % episode)

    def set_channel_status( self, channel):
        self.set_status( channel = _('Synchronizing %s') % channel)

    def set_episode_convert_status( self, episode, percentage):
        self.set_progress_sub_episode( int(percentage), 100)
        self.set_status( episode = _('Converting %s (%s%%)') % ( episode, str(percentage), ))
    
    def set_status( self, episode = None, channel = None, progressbar_text = None, title = None, header = None, body = None):
        if self.callback_status:
            gobject.idle_add( self.callback_status, episode, channel, progressbar_text, title, header, body)

    def sync_channel( self, channel, episodes = None, sync_played_episodes = True):
        if not channel.sync_to_devices and episodes == None or self.cancelled:
            return False

        if episodes == None:
            episodes = channel

        max = len(episodes)
        for pos, episode in enumerate(episodes):
            if self.cancelled:
                return False
            self.set_progress( pos, max)
            if episode.is_downloaded() and episode.file_type() in ( 'audio', 'video' ) and (sync_played_episodes or not episode.is_played()):
                if not self.add_episode_from_channel( channel, episode):
                    return False
        self.set_progress( pos, max)

        self.set_status( channel = _('Completed %s') % channel.title)
        time.sleep(1)
        
        return True

    def add_episode_from_channel( self, channel, episode):
        channeltext = channel.title

        if channel.is_music_channel:
            channeltext = _('%s (to "%s")') % ( channel.title, channel.device_playlist_name )
        
        gl = libgpodder.gPodderLib()
        if gl.config.on_sync_mark_played:
            log('Marking as played on transfer: %s', episode.url, sender = self)
            gl.history_mark_played(episode.url)

        self.set_episode_status( episode.title)
        self.set_channel_status( channeltext)

        return True

    def close( self, success = True, access_error = False, cleaned = False):
        try:
            self.set_status( channel = _('Writing data to disk'))
            os.system('sync')
        except:
            pass

        if self.callback_done:
            gobject.idle_add( self.callback_done, success, access_error, cleaned, self.errors)


class gPodder_iPodSync( gPodderSyncMethod):
    def __init__( self, callback_progress = None, callback_status = None, callback_done = None):
        if not ipod_supported():
            log( '(ipodsync) iPod functions not supported. (libgpod + eyed3 needed)')
        gl = libgpodder.gPodderLib()
        self.ipod_mount = gl.config.ipod_mount
        self.itdb = None
        self.playlist_name = 'gpodder' # name of playlist to sync to
        self.pl_master = None
        self.pl_podcasts = None
        gPodderSyncMethod.__init__( self, callback_progress, callback_status, callback_done)
    
    def open( self):
        if not ipod_supported():
            return False
        tries = 0
        while self.itdb == None and not self.cancelled:
            if os.path.isdir( self.ipod_mount):
                self.itdb = gpod.itdb_parse( str( self.ipod_mount), None)
            if self.itdb:
                self.itdb.mountpoint = str(self.ipod_mount)
                self.pl_master = gpod.sw_get_playlists( self.itdb)[0]
                self.pl_podcasts = gpod.itdb_playlist_podcasts( self.itdb)
                if not self.pl_master or not self.pl_podcasts:
                    return False
            else:
                header = _('Connect your iPod')
                body = _('To start the synchronization process, please connect your iPod to the computer.')
                if tries > 30:
                    return False
                elif tries > 15:
                    self.set_status( episode = _('Have you set up your iPod correctly?'), header = header, body = body)
                else:
                    self.set_status( channel = _('Please connect your iPod'), episode = _('Waiting for iPod'), header = header, body = body)

                time.sleep( 1)
                tries += 1

        return self.itdb != None

    def close( self, success = True, access_error = False, cleaned = False):
        if not ipod_supported():
            return False
        if self.itdb:
            self.set_status( channel = _('Saving iPod database'))
            gpod.itdb_write( self.itdb, None)
        self.itdb = None
        gPodderSyncMethod.close( self, success, access_error, cleaned)
        return True

    def remove_from_ipod( self, track, playlists):
        if not ipod_supported():
            return False
        log( '(ipodsync) Removing track from iPod: %s', track.title)
        status_text = _('Removing %s') % ( track.title, )
        self.set_status( channel = status_text)
        fname = gpod.itdb_filename_on_ipod( track)
        for playlist in playlists:
            try:
                gpod.itdb_playlist_remove_track( playlist, track)
            except:
                pass
        
        gpod.itdb_track_unlink( track)
        try:
            os.unlink( fname)
        except:
            # suppose we've already deleted it or so..
            pass
    
    def get_playlist_by_name( self, playlistname = 'gPodder'):
        if not ipod_supported():
            return False
        for playlist in gpod.sw_get_playlists( self.itdb):
            if playlist.name == playlistname:
                log( '(ipodsync) Found old playlist: %s', playlist.name)
                return playlist
        
        log( '(ipodsync) New playlist: %s', playlistname)
        new_playlist = gpod.itdb_playlist_new( str(playlistname), False)
        gpod.itdb_playlist_add( self.itdb, new_playlist, -1)
        return new_playlist

    def get_playlist_for_channel( self, channel):
        if channel.is_music_channel:
            return self.get_playlist_by_name( channel.device_playlist_name)
        else:
            return self.pl_podcasts

    def episode_is_on_ipod( self, channel, episode):
        if not ipod_supported():
            return False
        pl = self.get_playlist_for_channel( channel)
        if not pl:
            return False
        for track in gpod.sw_get_playlist_tracks( pl):
            if episode.title == track.title and channel.title == track.album:
                gl = libgpodder.gPodderLib()

                # Mark as played locally if played on iPod
                if track.playcount > 0:
                    log( 'Episode has been played %d times on iPod: %s', track.playcount, episode.title, sender = self)
                    gl.history_mark_played( episode.url)

                # Mark as played on iPod if played locally (and set podcast flags)
                self.set_podcast_flags( track, episode)

                return True
        
        return False
    
    def remove_tracks(self, tracklist=[]):
        for track in tracklist:
            log( '(ipodsync) Trying to remove: %s', track.title)
            self.remove_from_ipod(track, [self.pl_podcasts])

    def clean_playlist( self):
        if not ipod_supported():
            return False

        return gpod.sw_get_playlist_tracks(self.pl_podcasts)

    def set_podcast_flags( self, track, episode):
        if not ipod_supported():
            return False
        try:
            # Add blue bullet next to unplayed tracks on 5G iPods
            # (only if the podcast has not been played locally already
            gl = libgpodder.gPodderLib()
            if gl.history_is_played( episode.url):
                track.mark_unplayed = 0x01
                # Increment playcount if it's played locally
                # but still has zero playcount on iPod
                if track.playcount == 0:
                    track.playcount = 1
            else:
                track.mark_unplayed = 0x02

            # Podcast flags (for new iPods?)
            track.remember_playback_position = 0x01

            # Podcast flags (for old iPods?)
            track.flag1 = 0x02
            track.flag2 = 0x01
            track.flag3 = 0x01
            track.flag4 = 0x01

        except:
            log( '(ipodsync) Seems like your python-gpod is out-of-date.')
    
    def set_channel_art(self, track, local_filename):
        cover_filename = os.path.dirname( local_filename) + '/cover'
        if os.path.isfile( cover_filename):
            gpod.itdb_track_set_thumbnails( track, cover_filename)
            return True
        return False
            
    def set_cover_art( self, track, local_filename):
        if not ipod_supported():
            return False
        if get_coverart:
            tag = eyeD3.Tag()
            if tag.link( local_filename):
                if 'APIC' in tag.frames and len( tag.frames['APIC']) > 0:
                    # Try to guess the file extension (default to jpg)
                    extension = 'jpg'
                    if tag.frames['APIC'][0].mimeType == 'image/png':
                        extension = 'png'
                    cover_filename = '%s.cover.%s' ( local_filename, extension )
                    cover_file = open( cover_filename, 'w')
                    cover_file.write( tag.frames['APIC'][0].imageData)
                    cover_file.close()
                    gpod.itdb_track_set_thumbnails( track, cover_filename)
                    return True
            else:
                log( '(ipodsync) Error reading cover art for %s' % ( local_filename, ))
        # If we haven't yet found cover art, fall back to channel cover
        return self.set_channel_art( track, local_filename)

    def add_episode_from_channel( self, channel, episode):
        global DEFAULT_LENGTH
        global MPLAYER_COMMAND

        if not ipod_supported():
            return False

        gPodderSyncMethod.add_episode_from_channel( self, channel, episode)
        
        if self.episode_is_on_ipod( channel, episode):
            status_text = _('Already on iPod: %s') % ( episode.title, )
            self.set_status( episode = status_text)
            return True

        original_filename = str( episode.local_filename())
        local_filename = original_filename

        # Reserve 10 MiB for iTunesDB writing (to be on the safe side)
        RESERVED_FOR_ITDB = 1024*1024*10
        space_for_track = util.get_free_disk_space(self.ipod_mount) - RESERVED_FOR_ITDB
        needed = util.calculate_size(local_filename)

        if needed > space_for_track:
            log('Not enough space on %s: %s available, but need at least %s', self.ipod_mount, util.format_filesize(space_for_track), util.format_filesize(needed), sender = self)
            self.errors.append( _('Error copying %s: Not enough free disk space on %s') % (episode.title, self.ipod_mount))
            self.cancelled = True
            return False
        
        log( '(ipodsync) Adding item: %s from %s', episode.title, channel.title)
        if libconverter.converters.has_converter( os.path.splitext( original_filename)[1][1:]):
            log('(ipodsync) Converting: %s', original_filename)
            callback_status = lambda percentage: self.set_episode_convert_status( episode.title, percentage)
            local_filename = str( libconverter.converters.convert( original_filename, callback = callback_status))
            if not libtagupdate.update_metadata_on_file( local_filename, title = episode.title, artist = channel.title):
                log('(ipodsync) Could not set metadata on converted file %s', local_filename)
            self.set_episode_status( episode.title)
            if not local_filename:
                log('(ipodsync) Error while converting file %s', original_filename)
                return False

        # if we cannot get the track length, make an educated guess (default value)
        track_length = DEFAULT_LENGTH
        track_length_found = False

        if use_mplayer:
            try:
                log( 'Using mplayer to get file length', sender = self)
                mplayer_output = os.popen( MPLAYER_COMMAND % local_filename).read()
                track_length = int(float(mplayer_output[mplayer_output.index('ID_LENGTH'):].splitlines()[0][10:]) * 1000)
                track_length_found = True
            except:
                log( 'Warning: cannot get length for %s', episode.title, sender = self)
        else:
            log( 'Please try installing the "mplayer" package for track length detection.', sender = self)

        if not track_length_found:
            try:
                log( 'Using pymad to get file length', sender = self)
                mad_info = mad.MadFile( local_filename)
                track_length = mad_info.total_time()
                track_length_found = True
            except:
                log( 'Warning: cannot get length for %s', episode.title, sender = self)
        
        if not track_length_found:
            try:
                log( 'Using eyeD3 to get file length', sender = self)
                eyed3_info = eyeD3.Mp3AudioFile( local_filename)
                track_length = eyed3_info.getPlayTime() * 1000
                track_length_found = True
            except:
                log( 'Warning: cannot get length for %s', episode.title, sender = self)

        if not track_length_found:
            log( 'I was not able to find a correct track length, defaulting to %d', track_length, sender = self)
        
        track = gpod.itdb_track_new()
        
        track.artist = str(channel.title)
        self.set_podcast_flags( track, episode)
        
        # Add release time to track if pubDate is parseable
        ipod_date = email.Utils.parsedate(episode.pubDate)
        if ipod_date != None:
            try:
                # libgpod>= 0.5.x uses a new timestamp format
                track.time_released = gpod.itdb_time_host_to_mac(int(time.mktime(ipod_date)))
            except:
                # old (pre-0.5.x) libgpod versions expect mactime, so
                # we're going to manually build a good mactime timestamp here :)
                #
                # + 2082844800 for unixtime => mactime (1970 => 1904)
                track.time_released = int(time.mktime(ipod_date) + 2082844800)
        
        track.title = str(episode.title)
        track.album = str(channel.title)
        track.tracklen = int(track_length)
        track.description = str(episode.description)
        track.podcasturl = str(episode.url)
        track.podcastrss = str(channel.url)
        track.size = os.path.getsize( local_filename)
        track.filetype = 'mp3'
        # For audio podcasts (thanks to José Luis Fustel)
        try:
            track.mediatype = 0x00000004
        except:
            log( '(ipodsync) Seems like your python-gpod is out-of-date.')
            track.unk208 = 0x00000004

        gpod.itdb_track_add( self.itdb, track, -1)
        playlist = self.get_playlist_for_channel( channel)
        gpod.itdb_playlist_add_track( playlist, track, -1)

        try:
            self.set_cover_art( track, local_filename)
        except:
            log( 'Error setting cover art for "%s".', episode.title, sender = self)

        # dirty hack to get video working, seems to work
        for ext in video_extensions:
            if local_filename.lower().endswith( '.%s' % ext):
                track.filetype = 'm4v' # Doesn't seem to matter if it's mp4 or m4v
                try:
                    # documented on http://ipodlinux.org/ITunesDB
                    track.mediatype = 0x00000006
                except:
                    # for old libgpod versions, "mediatype" is "unk208"
                    log( '(ipodsync) Seems like your python-gpod is out-of-date.')
                    track.unk208 = 0x00000006

        # if it's a music channel, also sync to master playlist
        if channel.is_music_channel:
            gpod.itdb_playlist_add_track( self.pl_master, track, -1)

        if gpod.itdb_cp_track_to_ipod( track, local_filename, None) != 1:
            log( '(ipodsync) Could not add %s', episode.title)
        else:
            log( '(ipodsync) Added %s', episode.title)

        status_text = _('Done: %s') % ( episode.title, )
        self.set_status( episode = status_text)

        if local_filename != original_filename:
            log('(ipodsync) Removing temporary file: %s', local_filename)
            try:
                os.unlink( local_filename)
            except:
                log('(ipodsync) Could not remove temporary file %s', local_filename)

        return True


class gPodder_FSSync( gPodderSyncMethod):
    BUFFER = 1024*1024*1 # 1MB

    def __init__( self, callback_progress = None, callback_status = None, callback_done = None):
        gl = libgpodder.gPodderLib()
        self.destination = gl.config.mp3_player_folder
        gPodderSyncMethod.__init__( self, callback_progress, callback_status, callback_done)
        self.can_cancel = True

    def open( self):
        gpl = libgpodder.gPodderLib()
        return util.directory_is_writable( self.destination)
    
    def add_episode_from_channel( self, channel, episode):
        allowed_chars = set( string.lowercase + string.uppercase + string.digits + ' _.-')

        gPodderSyncMethod.add_episode_from_channel( self, channel, episode)

        gl = libgpodder.gPodderLib()

        if gl.config.fssync_channel_subfolders:
            # Add channel title as subfolder
            folder_src = channel.title
            folder = ''
            for ch in folder_src:
                if ch in allowed_chars:
                    folder = folder + ch
                else:
                    folder = folder + '_'
            folder = os.path.join( self.destination, folder)
        else:
            folder = self.destination

        from_file = episode.local_filename()

        filename_base = episode.sync_filename()

        # don't allow extremely long file names: cut the
        # filename at 50 chars (to make FAT-based drives happy)
        if len(filename_base) > 50:
            filename_base = filename_base[:50]

        to_file_src = filename_base + os.path.splitext( from_file)[1].lower()
        to_file = ''
        for ch in to_file_src:
            if ch in allowed_chars:
                to_file = to_file + ch
            else:
                to_file = to_file + '_'

        # dirty workaround: on bad (empty) episode titles,
        # we simply use the from_file basename
        # (please, podcast authors, FIX YOUR RSS FEEDS!)
        if os.path.splitext( to_file)[0] == '':
            to_file = os.path.basename( from_file)

        to_file = os.path.join( folder, to_file)

        try:
            os.makedirs( folder)
        except:
            pass

        if not os.path.exists( to_file):
            log( 'Copying: %s => %s', os.path.basename( from_file), to_file)
            if not self.copy_file_progress( from_file, to_file):
                return False

        return True

    def copy_file_progress( self, from_file, to_file):
        try:
            out_file = open( to_file, 'wb')
        except IOError, ioerror:
            self.errors.append( _('Error opening %s: %s') % ( ioerror.filename, ioerror.strerror, ))
            self.cancelled = True
            return False
        try:
            in_file = open( from_file, 'rb')
        except IOError, ioerror:
            self.errors.append( _('Error opening %s: %s') % ( ioerror.filename, ioerror.strerror, ))
            self.cancelled = True
            return False

        in_file.seek( 0, 2)
        bytes = in_file.tell()
        bytes_read = 0
        in_file.seek( 0)
        s = in_file.read( self.BUFFER)
        bytes_read += len(s)
        self.set_progress_sub_episode( bytes_read, bytes)
        while s:
            bytes_read += len(s)
            try:
                out_file.write( s)
            except IOError, ioerror:
                self.errors.append( ioerror.strerror)
                try:
                    out_file.close()
                except:
                    pass
                try:
                    log( 'Trying to remove partially copied file: %s' % ( to_file, ), sender = self)
                    os.unlink( to_file)
                    log( 'Yeah! Unlinked %s at least..'  % ( to_file, ), sender = self)
                except:
                    log( 'Error while trying to unlink %s. OH MY!' % ( to_file, ), sender = self)
                self.cancelled = True
                return False
            self.set_progress_sub_episode( bytes_read, bytes)
            s = in_file.read( self.BUFFER)
        out_file.close()
        in_file.close()

        return True
    
    def clean_playlist( self):
        folders = glob.glob( os.path.join( self.destination, '*'))
        for folder in range( len( folders)):
            if self.cancelled:
                return
            self.set_progress_overall( folder+1, len( folders))
            self.set_channel_status( os.path.basename( folders[folder]))
            self.set_status( episode = _('Removing files'))
            log( 'deleting: %s', folders[folder])
            shutil.rmtree( folders[folder])
            try:
                os.system('sync')
            except:
                pass


