#!/usr/bin/env python3

# Help message is automatically generated by argparse module. Invoke from a
# terminal to see all available options.
"""
Adapted from the bash script latex2pdf, this script tries to automatically run
pdflatex, bibtex, and makeindex enough times to get everything resolved. It
is quite solid for simple documents (i.e., a single .tex file with no \include
statements), but for larger projects, it may run some things more than it
needs to.

This script will check all of the included bibliography files to see if the
corresponding bbl need to be updated. This means that if you change a bib file
and then run this script, the bibliography changes will actually show up in
your document.

If you're using a package that requires multiple pdflatex runs (and it does not
output any standard warning message to tell you when you need to run pdflatex
again), then consider using the --min-runs option. This can make sure pdflatex
runs at least 2 times (or more).

Finally, there is a --paranoid option that will keep running pdflatex until
none of the aux files change. Note that if, for whatever reason, you are
printing a timestamp in the aux file, then this will break because aux files
never stop changing.

By default, if this script finishes with an error, the modified timestamp of
the pdf output is set to 1 second before the tex input so that the resulitng
pdf is "out of date" with respect to the tex file. This is useful when using
make, as it ensures the pdf will be remade after a second call to make. This
behavior is disabled with the --keep-pdf-timestamp flag.

Author: Michael Risbeck <risbeck@wisc.edu>
"""

import argparse
import collections
import hashlib
import functools
import os
import re
import shlex
import string
import subprocess
import sys
import tempfile
import traceback

# Helper function for parser.
def getfilecheck(ext=None, directory=False, exists=True, toAbs=False):
    """
    Returns a funcion to check whether inputs are valid files/directories.
    """
    def filecheck(s):
        s = str(s)
        if toAbs:
            try:
                s = os.path.abspath(s)
            except Exception as err: # Catch everything here.
                raise argparse.ArgumentTypeError("unable to get absolute "
                                                 "path") from err
        if ext is not None and not s.endswith(ext):
            raise argparse.ArgumentTypeError("must have '{}' "
                                             "extension".format(ext))
        if exists:
            if directory:
                if not os.path.isdir(s):
                    raise argparse.ArgumentTypeError("must be an existing "
                                                     "directory")
            elif not os.path.isfile(s):
                raise argparse.ArgumentTypeError("must be an existing file")
        return s
    return filecheck

# Build the parser in the global namespace so everybody has access.
parser = argparse.ArgumentParser(add_help=False, description=
        "runs pdflatex, bibtex, etc. on a .tex file to produce a pdf",
        epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

optargs = parser.add_argument_group("normal options")
optargs.add_argument("--help", help="print this help", action="help")
optargs.add_argument("--dir", help="working directory to run pdflatex, etc.",
                    type=getfilecheck(directory=True,toAbs=True),
                    default=os.getcwd())
optargs.add_argument("--texinputs", help="extra directories for tex inputs "
                     "(relative to DIR)", action="append", default=[])
optargs.add_argument("--bibinputs", action="append", default=[],
                     help="extra directories for bib inputs (relative to DIR)")
optargs.add_argument("--min-runs", help="minimum number of pdflatex runs",
                    type=int, default=1)
optargs.add_argument("--max-runs", help="maximum number of pdflatex runs",
                    type=int, default=4)
optargs.add_argument("--display", help="set display level (default: errors)",
                    choices=set(["all","some","errors","none"]),
                    default="errors")

advargs = parser.add_argument_group("advanced options")
advargs.add_argument("--pdflatex",help="shell command for pdflatex",
                     default="pdflatex")
advargs.add_argument("--flags",help="flags to pdflatex",
                     default="--synctex=1  --halt-on-error  --recorder")
advargs.add_argument("--paranoid",help="run until aux files do not change",
                     action="store_true")
advargs.add_argument("--check-all-aux", action="store_true",
                     help="consider all aux files in the directory, not just "
                     "those explicitly mentioned in the log file.")
advargs.add_argument("--recheck-aux", help="recheck for aux files after "
                     "each pdflatex run", action="store_true")
advargs.add_argument("--check-blx-timestamps", help="consider timestamps on "
                     "-blx.bib files.", action="store_true")
advargs.add_argument("--debug", help="print extra debugging information",
                     action="store_true")
advargs.add_argument("--show-cite-errors", help="display missing citations "
                     "as errors", action="store_true")
advargs.add_argument("--ignore-cite-errors", action="store_true",
                     help="ignore citation errors on final run")
advargs.add_argument("--keep-pdf-timestamp", action="store_true",
                     help="Don't adjust pdf timestamp on error (see below)")

reqargs = parser.add_argument_group("required arguments")
reqargs.add_argument("texfile",help="source .tex file",
                    type=getfilecheck(ext=".tex", toAbs=True))

# Don't show help for these options as they are either esoteric or can lead to
# very bad things.
parser.add_argument("--DEFAULT-FLAGS", help=argparse.SUPPRESS,
                    default="--interaction=nonstopmode")


# Some helper functions.
openlog = functools.partial(open, mode="r", encoding="ascii", errors="replace")


def errorout(message="Invalid Usage", doc=False, usage=True,
             crash=True, fromerr=None):
    """Prints help and exits by raising an exception."""
    if doc:
        parser.print_help()
    elif usage:
        parser.print_usage()
    if crash:
        raise RuntimeError(message) from fromerr


def getauxinfo(files=None, pdir=".", md5=False, ext=".aux"):
    """
    Returns a dictionary with AuxFile named tuples for each element of files.

    If files is None, gets a list of all files in pdir.
    """
    if files is None:
        files = os.listdir(pdir)
    files = getfullpaths(files, pdir)
    auxinfo = {}
    for f in filter(lambda f: f.endswith(ext), files):
        relpath = os.path.relpath(f,pdir)
        exists = os.path.isfile(f)
        if exists:
            timestamp = os.path.getmtime(f)
            bibdata = getbibdata(f)
            md5 = md5sum(f) if md5 else None
        else:
            timestamp = -float("inf")
            bibdata = tuple()
            md5 = None
        auxinfo[f] = AuxFile(relpath, exists, timestamp, bibdata, md5)
    return auxinfo


def getindexfiles(logfile):
    """
    Searches through a log file to find any index files.

    Returns a dict with values (input ext, output ext) and string keys.
    """
    indexes = {
        # output ext : (name, input ext.)
        "idx" : ("subject", "ind"),
        "ain" : ("author", "and"),
        "ctx" : ("citation", "cnd"),
    }
    found = {}
    regex = re.compile(r"Writing index file .*\.({})"
                       .format("|".join(indexes.keys())))
    with openlog(logfile) as log:
        for line in log:
            s = regex.search(line)
            if s is not None:
                inext = s.group(1)
                (name, outext) = indexes[inext]
                found[name] = (inext, outext)
    return found


def getmodifiedaux(old, new, md5=False):
    """
    Returns AuxFile for files changed in new.

    Files not in old, files strictly newer than their counterparts in old, or
    (if md5 is True), files whose md5 sums do not match in old.
    """
    changed = {}
    for (f, aux) in new.items():
        ischanged = (f not in old
                     or aux.timestamp > old[f].timestamp
                     or (md5 and aux.md5 != old[f].md5))
        if ischanged:
            changed[f] = aux
    return changed


def getauxfromlog(log, pdir=""):
    """
    Scans through the log file and looks for aux files.

    Returns a list of absolute paths to aux files.

    Only returns a given file if it exists (with relative paths taken relative
    to pdir).
    """
    # Get a nice filename for printing (not used anywhere else).
    rellog = os.path.relpath(log, os.path.abspath(pdir))
    if rellog.startswith(".."):
        rellog = log

    # Do actual work.
    aux = set()
    auxre = re.compile(r"\((\.?/.*?\.aux)")
    with openlog(log) as f:
        console.debug("Opening log file <{}>.", rellog)
        for line in f:
            for a in auxre.findall(line):
                a = os.path.normpath(os.path.join(pdir, a))
                if os.path.isfile(a):
                    aux.add(a)
    return list(aux)


def getbibdata(filename):
    """
    Searches filename for \bibdata{*} and returns bibliography files.
    """
    bibfiles = []
    bibdata = re.compile(r"\\bibdata\{(.*)\}")
    f = openlog(filename)
    for line in f:
        m = bibdata.match(line)
        if m is not None:
            for b in m.group(1).split(","):
                bibfiles.append(b.strip() + ".bib")
    return tuple(bibfiles)


def biboutofdate(auxinfo, pdir, includeblx=True, env=None):
    """
    Check timestamps on bib files to see if bbl files need to be remade.

    Returns a tuple whose first entry is True/False to say whether the
    bibliography is out of date, and whose second entry is a list of full
    bibliography filenames.
    """
    # Need to check any included bib files to see if they have been modified.
    bib = []
    for aux in auxinfo.values():
        for b in aux.bibdata:
            bib.append(b)
    if not includeblx:
        bib = list(filter(lambda b : not b.endswith("-blx.bib"), bib))
    if len(bib) == 0:
        # Nothing to do.
        outofdate = False
        bibfull = []
    else:
        # Get list of bib files and last modified timestamps.
        bibfull = kpsewhich(bib, pdir, env=env)
        bibtimes = [getmtime(f) for f in bibfull]
        newestbib = safemax(bibtimes)

        # Get a list of the .bbl files and timestamps.
        bblfull = [re.sub(".aux$",".bbl",f) for f in auxinfo]
        bbltimes = [getmtime(f, na=float("inf")) for f in bblfull]
        oldestbbl = safemin(bbltimes)

        # Check whether or not everything is in date.
        outofdate = (newestbib >= oldestbbl)
    return (outofdate, bibfull)


def kpsewhich(biblist, cwd=".", env=None):
    """
    Runs kpsewhich on a list of bib files and returns full paths for each.
    """
    kpse = subprocess.Popen(["kpsewhich"] + biblist, cwd=cwd,
                            stdout=subprocess.PIPE, env=env)
    (bibfullraw, kpsewhicherr) = kpse.communicate()
    bibfull = getfullpaths(bibfullraw.decode().split("\n"), pdir=cwd)
    return bibfull


def getmtime(f, na=-float("inf")):
    """
    Wrapper to os.path.getmtime, returning -inf if a file doesn't exist.

    The optional argument na controls what is returned for files that don't
    exist.
    """
    try:
        t = os.path.getmtime(f)
    except OSError:
        t = na
    return t


def getfullpaths(files, pdir=None, empty=False):
    """
    Gets full file names for each element of files."

    pdir is used as the prefix for relative paths. Set empty=True to still
    include empty strings in files. Otherwise, they are skipped.
    """
    minlen = 0 if empty else 1
    if pdir is None:
        pdir = os.getcwd()
    fullpaths = []
    for f in files:
        if len(f) >= minlen:
            fullpaths.append(os.path.normpath(os.path.join(pdir, f.strip())))
    return fullpaths


def safemax(x,empty=-float("inf")):
    """
    Maximum of list x with optional value in case x is empty (default: -inf).
    """
    if len(x) == 0:
        return empty
    else:
        return max(x)


def safemin(x,empty=float("inf")):
    """
    Minimum of list x with optional value in case x is empty (default: inf).
    """
    if len(x) == 0:
        return empty
    else:
        return min(x)


def checktotalslides(auxfile):
    """
    Scans an aux file for \inserttotalframenumber and returns number of frames.

    If aux file doesn't exist, or if there is no \inserttotalframenumber, then
    returns None.
    """
    retval = None
    if os.path.isfile(auxfile):
        with openlog(auxfile) as aux:
            for line in aux:
                m = re.search(r"\\inserttotalframenumber\s\{(\d+)\}", line)
                if m is not None:
                    retval = int(m.group(1))
                    console.debug("Found inserttotalframenumber: %s", retval)
    return retval


def md5sum(filename, block=128):
    """
    Returns md5 digest of a file.
    """
    md5 = hashlib.md5()
    with open(filename, "rb") as f:
        for chunk in iter(lambda: f.read(block*md5.block_size), b""):
            md5.update(chunk)
    return md5.hexdigest()


# Create a namedtuple for storing aux file information.
AuxFile = collections.namedtuple("AuxFile", ["relpath", "exists", "timestamp",
                                             "bibdata", "md5"])

class ErrorChecker:
    """Uses regexes to search for error messages in log files."""
    def __init__(self, defaultlevel="error"):
        """Initialize the object."""
        self.__checks = {}
        self.__Check = collections.namedtuple("Check", ["regex", "level",
                                                        "lines"])
        self.defaultlevel = defaultlevel

    def add(self, regex=None, *, level=None, lines=1, name=None):
        """
        Adds a new or modifies an existing regex to look for.

        The pattern string for the regex must be provided. Optionally, it can
        be given a string name that allows you to modify it later. Other
        options error decides whether the message indicates an error that
        should be printed, and if so, lines says how many lines from the log
        file should be printed.

        Returns the name given to the regex.
        """
        # Get name.
        if name is None:
            name = len(self.__checks)
        elif not isinstance(name, str):
            raise TypeError("name must be a str, not {}".format(type(name)))
        elif name in self.__checks:
            raise ValueError("name '{}' is already used".format(name))

        # Get default level.
        if level is None:
            level = self.defaultlevel

        # Add the new tuple and return name.
        self.__checks[name] = self.__Check(re.compile(regex), level, lines)
        return name

    def change(self, name, *, level=None, lines=None):
        """
        Modifies the options for a given regex.

        name should be the identifier given when originally added.
        """
        if name not in self.__checks:
            raise KeyError("Name '{}' is not known!".format(name))
        default = self.__checks[name]
        if level is None:
            level = default.level
        if lines is None:
            lines = default.lines
        self.__checks[name] = self.__Check(default.regex, level, lines)

    def search(self, logfile):
        """
        Searches logfile for the messages and returns errors and warnings.

        Return value is a dict whose keys are the levels of each error and
        whose values are lists of the error messages.
        """
        messages = collections.defaultdict(list)
        with openlog(logfile) as log:
            for line in log:
                for check in self.__checks.values():
                    m = check.regex.search(line)
                    if m is not None:
                        # Match found. Get any extra lines.
                        match = [line.strip("\n")]
                        for n in range(check.lines - 1):
                            match.append(next(log, "").strip("\n"))
                        match = "\n".join(match)
                        messages[check.level].append(match)
        return messages

# Regexes to search log file. Give a name to items that we may want to
# change to an error later.
logchecker = ErrorChecker(defaultlevel="warning")
logchecker.add("Warning:.*Rerun to get cross")
logchecker.add("Warning:.*Citation.*may have changed")
logchecker.add("Warning:.*Citation.*undefined", name="undefcite")
logchecker.add(r"Warning: Reference `.*' on page \d+ undefined",
               name="undefref")
logchecker.add("Warning:.*There were undefined (references|citations)",
               name="undef")
logchecker.add(r"No file .*\.(bbl|toc|[aic]nd)")
logchecker.add("Package rerunfilecheck Warning: File.*has changed")
logchecker.add("LaTeX Error:", level="fatal")
logchecker.add("! Undefined control sequence", lines=2, level="fatal")
logchecker.add("!  ==> Fatal error occurred", level="fatal")
logchecker.add("! Missing [{}] inserted", lines=5, level="fatal")
logchecker.add("! Package.*Error", level="fatal")

# Regexes to search bib file.
blgchecker = ErrorChecker(defaultlevel="error")
blgchecker.add(r"Repeated entry---line \d* of file", lines=4)
blgchecker.add("Warning--I didn't find a database entry for")
blgchecker.add(r"I was expecting a .* or a .*---line \d* of file", lines=5)
blgchecker.add("Warning--entry type for .* isn't style-file defined", lines=2)
blgchecker.add("I couldn't open database file", lines=4)

class StatusConsole:
    """Lightweight logging class to print progress."""
    def __init__(self, status=True, errors=True, debug=False, critical=True):
        """Initializes and sets logging level."""
        self.show_status = status
        self.show_errors = errors
        self.show_debug = debug
        self.show_critical = critical

        # Set printing formats.
        self.formats = collections.defaultdict(lambda : "{}")
        self.formats["debug"] = " |  {}"
        self.formats["error"] = " !! {}"
        self.formats["status"] = "*** {} ***"
        self.formats["critical"] = ">>> {}"

    def multiprint(self, messages):
        """Prints messages from the output of ErrorChecker.search()."""
        self.multiignore(messages["ignore"])
        self.multidebug(messages["warning"])
        if self.show_debug or len(messages["fatal"]) == 0:
            self.multierror(messages["error"])
        self.multierror(messages["fatal"], "fatal errors")

    def status(self, message, *args, **kwargs):
        """Prints a status message."""
        if self.show_status or self.show_debug:
            message = message.format(*args, **kwargs)
            self._printlines(message.split("\n"), fmt="status")

    def debug(self, message, *args, **kwargs):
        """Prints a debug message."""
        if self.show_debug:
            message = message.format(*args, **kwargs)
            self._printlines(message.split("\n"), fmt="debug")

    def debugstatus(self, message, *args, **kwargs):
        """Prints a status message that only shows in debug mode."""
        if self.show_debug:
            message = message.format(*args, **kwargs)
            self._printlines(message.split("\n"), fmt="status")

    def error(self, message, *args, **kwargs):
        """Prints an error message."""
        if self.show_errors or self.show_debug:
            message = message.format(*args, **kwargs)
            self._printlines(message.split("\n"), fmt="error")

    def multierror(self, errors, etype="errors"):
        """Prints a bulleted list of multiple errors."""
        if (self.show_errors or self.show_debug) and len(errors) > 0:
            self.error("Found the following {}:", etype)
            self._printbulleted(errors, fmt="error")

    def multidebug(self, debugs):
        """Prints a bulleted list of multiple debug statements."""
        if self.show_debug and len(debugs) > 0:
            self.debug("Found the following warnings:")
            self._printbulleted(debugs, fmt="debug")

    def multiignore(self, ignores):
        """Prints a bulleted list of ignored messages."""
        if self.show_debug and len(ignores) > 0:
            self.debug("Ignoring the following messages:")
            self._printbulleted(ignores, fmt="debug")

    def critical(self, message, *args, **kwargs):
        """Prints a critical message."""
        if self.show_critical or self.show_debug:
            message = message.format(*args, **kwargs)
            self._printlines(message.split("\n"), fmt="critical")

    def _printbulleted(self, errors, bullet="  > ", fmt="error",
                       lines=None):
        """Prints a list of multiline strings using bullets."""
        lines = []
        for err in errors:
            err = (bullet + err).replace("\n", "\n" + " "*len(bullet))
            lines = lines + err.split("\n")
        self._printlines(lines, fmt=fmt)

    def _printlines(self, lines, fmt=None):
        """Prints multiple lines, formatting each using fmt."""
        fmt = self.formats[fmt]
        for line in lines:
            print(fmt.format(line))
console = StatusConsole() # Global instance for printing.


# Class to properly format the citation index.
class CitationIndex:
    """Functions to help make a clean citation index."""
    def __init__(self):
        """Compiles all necessary regexes."""
        self.entryre = re.compile(r"\\indexentry \{\{(?P<author>.*?)\}\\ "
                                   "(?P<year>.*?)\}\{(?P<page>.*?)\}")
        self.tokenre = re.compile(r"\\[^ \{] ?")

        self.spaceafterre = re.compile(r"\{\s+")
        self.spacebeforere = re.compile(r"\s+\}")

        self.keeptext = set(string.ascii_letters + string.digits + " ")

        self.replacements = [
            (r"\OE", "OE"),
            (r"\aa", "a"),
            (r"\O", "O"),
            (r"\o", "o"),
            (r"\ss", "s"),
            (r"\ae", "ae"),
            (r"\AA", "A"),
            (r"\L", "L"),
            (r"\l", "l"),
            (r"\oe", "oe"),
            (r"\ae", "AE"),
            ("~", " ")
        ]

    def nobracespace(self, s):
        """
        Gets rid of whitespace immediately inside braces.
        """
        s = self.spacebeforere.sub("{", s)
        s = self.spaceafterre.sub("}", s)
        return s

    def keep(self, s):
        """Returns true or false whether s is in KEEPTEXT."""
        return (s in self.keeptext)

    def purify(self, name):
        """
        Returns "purified" version of name.
        """
        name = self.nobracespace(name)
        for (find, repl) in self.replacements:
            name = name.replace(find, repl)
        name = self.tokenre.sub("", name)
        name = str(filter(self.keep, name))
        return name.upper()

    def clean(self, infilename, outfilename=None):
        """
        Makes a clean citationindex from infile.
        """
        # Choose output file.
        if outfilename is None:
            indir = os.path.split(infilename)[0]
            kwargs = dict(dir=indir, prefix="authorindex_", delete=False)
            getoutfile = lambda : tempfile.NamedTemporaryFile("w", **kwargs)
        else:
            getoutfile = lambda : open(outfilename, "w")

        # Do main loop.
        with open(infilename, "r") as infile, getoutfile() as outfile:
            console.debug("opened file '{}' for citationindex.", outfile.name)
            for (i, line) in enumerate(infile):
                match = self.entryre.search(line)
                if match is not None:
                    name = match.group("author")
                    nicename = self.purify(name)
                    year = match.group("year")
                    niceyear = self.purify(year)
                    page = match.group("page")

                    key = r"{%s}\ %s" % (name, year)
                    nicekey = " ".join([nicename, niceyear])

                    entry = r"\indexentry{%s@%s}{%s}" % (nicekey, key, page)
                    outfile.write(entry + "\n")
                else:
                    raise RuntimeError("No citation index entry on line "
                                       "{:d} of '{}'. Is the citation index "
                                       "corrupt?".format(i, infilename))
            if outfilename is None:
                outfiletmp = outfile.name
            else:
                outfiletmp = None

        # Overwrite input file if no output file name was given.
        if outfiletmp is not None:
            console.debug("renaming '{}' to '{}'", outfiletmp, infilename)
            os.rename(outfiletmp, infilename)
citationindex = CitationIndex() # Create a global instance for use.


# Main function.
def main(*args):
    """
    Gets command line arguments as a list and runs the main script.

    Note that the name of the script is not needed as an argument; thus, to
    use arguments passed from the command line, this should be called as
    main(*sys.argv[1:]).
    """
    # Process arguments.
    options = vars(parser.parse_args(args))

    # Break out some arguments.
    displayOptions = {
        #     (some display, error display, stdout)
        "none" : (False, False, os.devnull),
        "some" : (True, False, os.devnull),
        "errors" : (True, True, os.devnull),
        "all" : (True, True, None),
    }
    (somedisplay, errordisplay, stdout) = displayOptions[options["display"]]
    if stdout is not None:
        stdout = open(stdout, "w")

    # Set console options for printing.
    debug = options["debug"]
    console.show_status = somedisplay
    console.show_errors = errordisplay
    console.show_debug = debug
    logchecker.change("undefcite",
                      level="error" if options["show_cite_errors"]
                             else "warning")

    # Grab some other options.
    fullfilename = options["texfile"]
    fullbuilddir = options["dir"]
    maxruns = options["max_runs"] + options["paranoid"] # Extra for paranoid.
    minruns = options["min_runs"]

    # Now it's time to actually run pdflatex and other stuff.
    (filedir, basefilename) = os.path.split(fullfilename)
    basefilename = os.path.splitext(basefilename)[0] # Remove .tex extension.

    console.debugstatus("Source file: {}", os.path.relpath(fullfilename,
                                                           fullbuilddir))

    extrafileext = ["log", "idx", "aux", "ind", "tex", "ctx", "ain", "and",
                    "cnd", "pdf"]
    extrafile = {}
    for ext in extrafileext:
        extrafile[ext] = os.path.join(fullbuilddir,basefilename + "." + ext)

    # Figure out pdflatex arguments.
    pdflatexargs = ([options["pdflatex"]]
                    + shlex.split(options["flags"] + " "
                                  + options["DEFAULT_FLAGS"]))

    # Also get environment variables for pdflatex. We need to change a variable
    # so that long filenames aren't broken across lines in the log file. We
    # use the same environment for bibtex as well.
    pdflatexenv = dict(os.environ, max_print_line="2048")
    for k in ["texinputs", "bibinputs"]:
        if len(options[k]) > 0:
            K = k.upper()
            pdflatexenv[K] = ":".join([pdflatexenv.get(K, "")] + options[k])
            console.debug("Set {} to '{}'", K, pdflatexenv[K])

    # Check aux file to see if there is an \inserttotalframenumber anywhere.
    beamer_numslides = checktotalslides(extrafile["aux"])

    # Now start the main loop.
    keepgoing = True
    auxinfo = {}
    indexfiles = {}
    for runcount in range(1, maxruns + 1):
        # Run bibtex if any bib files are found.
        for (f, a) in auxinfo.items():
            if len(a.bibdata) > 0:
                # We need to make sure each file is passed with a relative
                # filename or else bibtex will complain because it doesn't want
                # to open any files outside of its working directory. So, we
                # check if the input is an absolute path, and if so, we make it
                # a relative path.
                if a.relpath.startswith(".."):
                    console.critical("Warning: file <{}> is not within "
                                     "directory <{}>. Bibtex will likely "
                                     "error.", a.relpath, fullbuilddir)

                console.status("running bibtex on <{}>", a.relpath)
                bibtex = subprocess.Popen(["bibtex",a.relpath],
                                          cwd=fullbuilddir, stdout=stdout,
                                          env=pdflatexenv)
                bibtex.wait()
                if bibtex.returncode != 0:
                    console.error("bibtex error [Code {:d}].",
                                  bibtex.returncode)
                    if errordisplay:
                        blgfile = re.sub("\\.aux$", ".blg", a.relpath)
                        absblgfile = os.path.join(fullbuilddir, blgfile)
                        try:
                            blgmessages = blgchecker.search(absblgfile)
                        except IOError as err:
                            errorout(message="Fatal bibtex error searching "
                                     "file {} ".format(blgfile), usage=False,
                                     fromerr=err)
                        console.multiprint(blgmessages)

        # Run makeindex on any indices that are found. Note special behavior
        # for author index.
        for (indextype, (inext, outext)) in indexfiles.items():
            if os.path.isfile(extrafile[inext]):
                console.debug("building {} file.", outext)

                # If this is an author index, we have to run authorindex first.
                if indextype == "author":
                    console.status("running authorindex")
                    auxfiles = list(auxinfo.keys())
                    args = ["authorindex", "-i", "-r", basefilename] + auxfiles
                    makeauthor = subprocess.Popen(args, cwd=fullbuilddir,
                                                  stdout=stdout, stderr=stdout)
                    makeauthor.wait()

                elif indextype == "citation":
                    citationindex.clean(extrafile[inext])

                # Now run makeindex.
                    console.status("cleaning citation index")
                console.status("running makeindex")
                [relin, relout] = [os.path.relpath(extrafile[k], fullbuilddir)
                                   for k in [inext, outext]]
                if relin.startswith(".."):
                   console.critical("Warning: file <{}> is not within "
                                    "directory <{}>. Makeindex will likely "
                                    "error.", relin, fullbuilddir)
                args = ["makeindex", "-o", relout, relin]
                makeindex = subprocess.Popen(args, cwd=fullbuilddir,
                                             stdout=stdout, # Makeindex uses
                                             stderr=stdout) # stderr
                makeindex.wait()


        # Get a list of all aux files in the build directory and information
        # including timestamps, any \bibdata entries, and possibly md5 sums.
        # We get the list of files from the previous auxinfo dict. If this is
        # the first run, we need a guess for all the auxiliary files we might
        # need. We first take all the files currently in the build directory.
        # Then, if the log file exists, we search for additional aux files. If
        # this is a subsequent run, we can just use the files found before.
        if runcount == 1:
            auxfiles = [extrafile["aux"]] + os.listdir(fullbuilddir)
            if os.path.isfile(extrafile["log"]):
                auxfiles += getauxfromlog(extrafile["log"], pdir=fullbuilddir)
        else:
            auxfiles = list(auxinfo.keys())
        oldauxinfo = getauxinfo(auxfiles, pdir=fullbuilddir,
                                md5=options["paranoid"])

        # Now run pdflatex.
        console.status("running pdflatex ({:d})", runcount)

        pdflatex = subprocess.Popen(pdflatexargs + [fullfilename],
                                    cwd=fullbuilddir, stdout=stdout,
                                    env=pdflatexenv)
        pdflatex.wait()
        keepgoing = (pdflatex.returncode != 0)
        if keepgoing:
            console.error("pdflatex error [Code {:d}]. Check log.",
                          pdflatex.returncode)

        # Check log file for any errors or warnings. If final run, display
        # undefined citations as errors.
        if runcount == maxruns:
            citeerror = "ignore" if options["ignore_cite_errors"] else "error"
            for k in ["undef", "undefref", "undefcite"]:
                logchecker.change(k, level=citeerror)
        logmessages = logchecker.search(extrafile["log"])
        console.multiprint(logmessages)
        if any(len(logmessages[k]) > 0 for k in ["warning", "error", "fatal"]):
            keepgoing = True

        # Update info for aux files.
        auxfiles = [extrafile["aux"]] + getauxfromlog(extrafile["log"],
                                                      pdir=fullbuilddir)
        if options["check_all_aux"]:
            auxfiles += os.listdir(fullbuilddir)
        newauxinfo = getauxinfo(auxfiles, pdir=fullbuilddir,
                                md5=options["paranoid"])
        auxinfo = getmodifiedaux(oldauxinfo, newauxinfo,
                                 md5=options["paranoid"])

        # If first time, check log file to get auxiliary files with
        # bibliographies. Also check to see if the number of slides has
        # changed.
        if runcount == 1 or options["recheck_aux"]:
            kwargs = dict(includeblx=options["check_blx_timestamps"],
                          env=pdflatexenv)
            (outofdate, fullbib) = biboutofdate(auxinfo, fullbuilddir,
                                                **kwargs)
            if outofdate:
                keepgoing = True
                console.debug("bbl files out of date. Need to rebuild.")

            if (beamer_numslides is not None) or options["recheck_aux"]:
                new_beamer_numslides = checktotalslides(extrafile["aux"])
                if new_beamer_numslides != beamer_numslides:
                    beamer_numslides = new_beamer_numslides
                    keepgoing = True
                    console.debug("Number of slides has changed.")

        # Look through log to get list of index files.
        indexfiles = getindexfiles(extrafile["log"])

        # Now loop through aux files to see if bibinfo is different. Also,
        # check md5 if the paranoid flag.
        for (f, a) in auxinfo.items():
            if len(a.bibdata) > 0:
                if f in oldauxinfo and oldauxinfo[f].bibdata != a.bibdata:
                    keepgoing = True
                    console.debug("bibdata changed in '{}'.", a.relpath)
            if not keepgoing and options["paranoid"]:
                if f not in oldauxinfo:
                    keepgoing = True
                    console.debug("aux file '{}' is new.", a.relpath)
                elif oldauxinfo[f].md5 != a.md5:
                    keepgoing = True
                    console.debug("md5 of '{}' has changed.", a.relpath)

        # Check all index files and make sure they are older than the tex file.
        for (_, ext) in indexfiles.values():
            textime = getmtime(extrafile["tex"])
            indextime = getmtime(extrafile[ext], na=float("inf"))
            if textime >= indextime:
                keepgoing = True
                console.debug("{} file is out of date.", ext)

        # Make sure we've at least run minruns times.
        if runcount < minruns:
            keepgoing = True
            console.debug("Have not met minimum runs ({:d} of {:d})",
                          runcount, minruns)

        # If there's nothing left to do, we can go ahead and break.
        if not keepgoing:
            console.debug("No issues found. Stopping.")
            break

    # Now see if everything worked properly.
    if keepgoing:
        if not options["keep_pdf_timestamp"]:
            # Change modification time for pdf file.
            if all(os.path.isfile(extrafile[ext]) for ext in ["pdf", "tex"]):
                mtime = os.path.getmtime(extrafile["tex"]) - 1
                os.utime(extrafile["pdf"], (mtime, mtime))
        errorout("Errors persist after {:d} runs".format(runcount),
                 doc=False, usage=False)
    else:
        console.debugstatus("Successful completion.")


# Finally, run main file.
if __name__ == "__main__":
    try:
        main(*sys.argv[1:])
        sys.exit(0)
    except Exception as err:
        if console.show_debug:
            traceback.print_exc()
        console.critical(str(err))
        sys.exit(1)
