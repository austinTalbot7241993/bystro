use 5.10.0;
use strict;
use warnings;
# TODO: Also support reading zipped files (right now only gzip files)
package Seq::Role::IO;

our $VERSION = '0.001';

# ABSTRACT: A moose role for all of our file handle needs
# VERSION

use Mouse::Role;

use PerlIO::utf8_strict;
use PerlIO::gzip;
use File::Which qw/which/;

use Path::Tiny;
use Try::Tiny;
use DDP;
use Scalar::Util qw/looks_like_number/;
with 'Seq::Role::Message';
# tried various ways of assigning this to an attrib, with the intention that
# one could change the taint checking characters allowed but this is the simpliest
# one that worked; wanted it precompiled to improve the speed of checking
my $taintCheckRegex = qr{\A([\+\,\.\-\=\:\/\t\|\s\w\d/]+)\z};

has taintCheckRegex => (
  is => 'ro',
  lazy => 1,
  init_arg => undef,
  default => sub{ $taintCheckRegex },
);

has delimiter => (
  is => 'ro',
  lazy => 1,
  default => '\t',
  writer => '_setDelimiter',
);

my $tar = which('tar');
my $gzip = which('pigz') || which('gzip');
my $lz4 = which('lz4');
say "LZ4 is $lz4";
# Without this, pigz -d -c issues many system calls (futex)
# Thanks to Meltdown, this slows decompression substantially
# Up to 1 core will be used solely for meltdown-related overhead
# So disable mutli-threading
# For compression, tradeoff still worth it
my $dcmpArgs = '-d -c';
if($gzip =~ /pigz/) {
 $dcmpArgs = "-p 1 $dcmpArgs";
}

my $tarCompressed = "$tar --use-compress-program=$gzip";

has gzip => (is => 'ro', isa => 'Str', init_arg => undef, lazy => 1, default => sub {$gzip});
has decompressArgs => (is => 'ro', isa => 'Str', init_arg => undef, lazy => 1, default => sub {$dcmpArgs});

#@param {Path::Tiny} $file : the Path::Tiny object representing a single input file
#@param {Str} $innerFile : if passed a tarball, we will want to stream a single file within
#@return file handle
sub getReadFh {
  my ( $self, $file, $innerFile, $errCode) = @_;

  # By default, we'll return an error, rather than die-ing with log
  # We wont be able to catch pipe errors however
  if(!$errCode) {
    $errCode = 'error';
  }

  if(ref $file ne 'Path::Tiny' ) {
    $file = path($file)->absolute;
  }

  my $filePath = $file->stringify;

  if (!$file->is_file) {
    my $err = "$filePath does not exist for reading";
    $self->log($errCode, $err);

    return ($err, undef, undef);
  }

  if($innerFile) {
    my ($err, $compressed, $command) = $self->getInnerFileCommand($filePath, $innerFile, $errCode);

    if($err) {
      return ($err, undef, undef);
    }

    $err = $self->safeOpen(my $fh, '-|', $command, $errCode);

    # If an innerFile is passed, we assume that $file is a path to a tarball

    return ($err, $compressed, $fh);
  }

  my $compressed = 0;
  my ($err, $fh);
  if($filePath =~ /[.]gz$/ || $filePath =~ /[.]bgz$/ || $filePath =~ /[.]zip$/ || $filePath =~ /[.]lz4$/) {
    $compressed = 1;
    #PerlIO::gzip doesn't seem to play nicely with MCE, reads random number of lines
    #and then exits, so use gunzip, standard on linux, and faster
    if($filePath =~ /[.]lz4$/) {
      $err = $self->safeOpen($fh, '-|', "$lz4 -d -c \"$filePath\"", $errCode);
    } else {
      $err = $self->safeOpen($fh, '-|', "$gzip $dcmpArgs \"$filePath\"", $errCode);
    }
  } else {
    $err = $self->safeOpen($fh, '<', $filePath, $errCode);
  };

  return ($err, $compressed, $fh);
}

sub getInnerFileCommand {
  my ($self, $filePath, $innerFile, $errCode) = @_;

  if(!$errCode) {
    $errCode = 'error';
  }

  my $compressed = $innerFile =~ /[.]gz$/ || $innerFile =~ /[.]bgz$/ || $innerFile =~ /[.]zip$/ || $filePath =~ /[.]lz4$/;

  my $innerCommand;
  if($filePath =~ /[.]lz4$/) {
    $innerCommand = $compressed ? "\"$innerFile\" | $lz4 -d -c -" : "\"$innerFile\"";
  } else {
    $innerCommand = $compressed ? "\"$innerFile\" | $gzip $dcmpArgs -" : "\"$innerFile\"";
  }

  # We do this because we have not built in error handling from opening streams

  my $err;
  my $command;
  my $outerCompressed;
  if($filePath =~ /[.]tar[.]gz$/) {
    $outerCompressed = 1;
    $command = "$tarCompressed -O -xf \"$filePath\" $innerCommand";
  } elsif($filePath =~ /[.]tar$/) {
    $command = "$tar -O -xf \"$filePath\" $innerCommand";
  } else {
    $err = "When inner file provided, must provde a parent file.tar or file.tar.gz";

    $self->log($errCode, $err);

    return ($err, undef, undef);
  }

  # From a size standpoint a tarball and a tar file whose inner annotation is compressed are similar
  # since the annotation dominate
  $compressed = $compressed || $outerCompressed;
  # If an innerFile is passed, we assume that $file is a path to a tarball

  return ($err, $compressed, $command);
}

# Is the file a single compressed file
sub isCompressedSingle {
  my ($self, $filePath) = @_;

  my $basename = path($filePath)->basename();

  if($basename =~ /tar[.]gz$/) {
    return 0;
  }

  if($basename =~ /[.]gz$/ || $basename =~ /[.]bgz$/ || $basename =~ /[.]zip$/ ) {
    return "gzip";
  }

  if($basename =~ /[.]lz4$/) {
    return "lz4";
  }

  return "";
}

sub getCompressProgWithArgs {
  my ($self, $filePath) = @_;

  my $ext = $self->isCompressedSingle($filePath);
  
  if(!$ext) {
    return "cat";
  }
  if($ext eq 'gzip') {
    return "$gzip $dcmpArgs";
  }
  if($ext eq 'lz4') {
    return "$lz4 -d -c";
  }
}

# TODO: return error if failed
sub getWriteFh {
  my ( $self, $file, $compress, $errCode ) = @_;

  # By default, we'll return an error, rather than die-ing with log
  # We wont be able to catch pipe errors however
  if(!$errCode) {
    $errCode = 'error';
  }

  my $err;

  if(!$file) {
    $err = 'get_fh() expected a filename';
    $self->log($errCode, $err);

    return ($err, undef);
  }

  my $fh;
  my $hasGz = $file =~ /[.]gz$/ || $file =~ /[.]bgz$/ || $file =~ /[.]zip$/;
  my $hasLz4 = $file =~ /[.]lz4$/;
  if ( $hasGz || $hasLz4 || $compress) {
    if($hasLz4 || $compress =~ /[.]lz4$/) {
      # open($fh, ">:gzip", $file) or die $self->log('fatal', "Couldn't open $file for writing: $!");
      say STDERR "DOING LZ4 $lz4 -c > $file";
      $err = $self->safeOpen($fh, "|-", "$lz4 -c > $file", $errCode);
    } else {
      $err = $self->safeOpen($fh, "|-", "$gzip -c > $file", $errCode);
    }
    
  } else {
    $err = $self->safeOpen($fh, ">", $file, $errCode);
  }

  return ($err, $fh);
}

# Allows user to return an error, dies with loggin by default
sub safeSystem {
  my ($self, $cmd, $errCode) = @_;

  my $return = system($cmd);

  if($return > 0) {
    $self->log($errCode || 'debug', "Failed to execute $cmd. Return: $return, due to: $! ($?)");
    return $!;
  }

  return;
}

# Allows user to return an error; dies with logging by default
sub safeOpen {
  #my ($self, $fh, $operator, $operand, $errCode) = @_;
  #    $_[0], $_[1], $_[2],   $_[3], $_[4]

  # In some cases, file attempting to be read may not have been flushed
  # Clearest case is Log::Fast
  my $err = $_[0]->safeSystem('sync');

  # Modifies $fh/$_[1] by reference
  if($err || !open($_[1], $_[2], $_[3])) {
    $err = $err || $!;

    #$self    #$errCode                      #$operand
    $_[0]->log($_[4] || 'debug', "Couldn't open $_[3]: $err ($?)");
    return $err;
  }

  return;
}

sub safeClose {
  my ($self, $fh, $errCode) = @_;

  my $err = $self->safeSystem('sync');

  if ($err) {
    $self->log($errCode || 'debug', "Couldn't sync before close due to: $err");
    return $err;
  }

  if (!close($fh)) {
    $self->log($errCode || 'debug', "Couldn't close due to: $! ($?)");
    return $!;
  }

  return;
}

sub getCleanFields {
  my ( $self, $line ) = @_;

  chomp $line;
  if ( $line =~ m/$taintCheckRegex/xm ) {
    my @out;

    push @out, split $self->delimiter, $1;

    return \@out;
  }

  return undef;
}

sub getLineEndings {
  return $/;
}

sub setLineEndings {
  my ($self, $firstLine) = @_;

  if($firstLine =~ /\r\n$/) {
    $/ = "\r\n";
  } elsif($firstLine =~ /\n$/) {
    $/ = "\n";
  } elsif($firstLine =~ /\015/) {
    # Match ^M (MacOS style line endings, which Excel outputs on Macs)
    $/ = "\015";
  } else {
    return "Cannot discern line endings: Not Mac, Unix, or Windows style";
  }

  return "";
}

sub checkDelimiter {
  my ($self, $line) = @_;

  if($line =~ /^\s*\S+\t\S+/) {
    return 1;
  }

  return 0;
}

sub setDelimiter {
  my ($self, $line) = @_;

  if($line =~ /^\s*\S+\t\S+/) {
    $self->_setDelimiter('\t');
  } elsif ($line =~ /^\s*\S+,\S+/) {
    $self->_setDelimiter(',');
  } else {
    return "Line is not tab or comma delimited";
  }

  return "";
}

sub makeTarballName {
  my ($self, $baseName, $compress) = @_;

  return $baseName . ($compress ? '.tar.gz' : '.tar');
}

# Assumes if ref's are passed for dir, baseName, or compressedName, they are path tiny
sub compressDirIntoTarball {
  my ($self, $dir, $tarballName) = @_;

  if(!$tar) { 
    $self->log( 'warn', 'No tar program found');
    return 'No tar program found';
  }

  if(ref $dir) {
    $dir = $dir->stringify;
  }

  if(!$tarballName) {
    $self->log('warn', 'must provide baseName or tarballName');
    return 'Must provide baseName or tarballName';
  }

  if(ref $tarballName) {
    $tarballName = $tarballName->stringify;
  }

  $self->log( 'info', 'Compressing all output files' );

  my @files = glob $dir;

  if ( !@files) {
    $self->log( 'warn', "Directory is empty" );
    return 'Directory is empty';
  }

  my $tarProg = $tarballName =~ /tar.gz$/ ? $tarCompressed : $tar;
  my $tarCommand = sprintf("cd %s; $tarProg --exclude '.*' --exclude %s -cf %s * --remove-files",
    $dir,
    $tarballName, #and don't include our new compressed file in our tarball
    $tarballName, # the name of our tarball
  );

  $self->log('debug', "compress command: $tarCommand");

  my $err = $self->safeSystem($tarCommand);

  return $err;
}

sub getCompressedFileSize {
  my ($self, $filePath) = @_;

  my $extType = $self->isCompressedSingle($filePath);
  if(!$extType) {
    return ('Expect compressed file', undef);
  }

  my $gzProg = $extType eq 'gzip' ? $gzip : $lz4;

  my $err = $self->safeOpen(my $fh, "-|", "$gzProg -l " . path($filePath)->stringify);

  if($err) {
    return ($err, undef);
  }

  <$fh>;
  my $sizeLine = <$fh>;

  chomp $sizeLine;
  my @sizes = split(" ", $sizeLine);

  #pigz return 0? when file is larger than 4GB, gzip returns 0
  if(!$sizes[1] || !looks_like_number($sizes[1])) {
    return (undef, 0);
  }

  return (undef, $sizes[1]);
}

# returns chunk size in kbytes
sub getChunkSize {
  my ($self, $filePath, $parts, $min, $max) = @_;

  # If given 0
  $parts ||= 1;

  if(!$min) {
    $min = 512;
  }

  if(!$max) {
    $max = 32768;
  }

  my $size = path($filePath)->stat()->size;

  # Files range from ~ 10 - 60x compression ratios; but large files don't report
  # correct ratios (> 4GB)
  # If file is < 4GB it will be reported correctly
  if($self->isCompressedSingle($filePath)) {
    my ($err, $compressedSize) = $self->getCompressedFileSize($filePath);

    if($err) {
      return ($err, undef);
    }

    $size = $compressedSize == 0 ? $size * 30 : $compressedSize;
  }

  my $chunkSize = CORE::int($size / ($parts * 4096));

  if ($chunkSize < $min) {
    return (undef, $min);
  }

  # Cap to make sure memory usage doesn't grow uncontrollably
  if ($chunkSize > $max) {
    return (undef, $max);
  }

  return (undef, $chunkSize);
}

no Mouse::Role;

1;
