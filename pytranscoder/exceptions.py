class SizeNotConvertible(Exception):
    def __init__(self, file_size):
        message = f'This size {file_size} can not be converted'
        self.message = message
        super(SizeNotConvertible, self).__init__(self.message)


class WrongSizeType(Exception):
    def __init__(self, file_size):
        message = f'The provided size {file_size} is not an integer and can not be converted'
        self.message = message
        super(WrongSizeType, self).__init__(self.message)


class ErrorSizeTextConversion(Exception):
    def __init__(self, file_size):
        message = f'Can not get text for he provided size: {file_size}'
        self.message = message
        super(ErrorSizeTextConversion, self).__init__(self.message)


class ErrorEmptyFilePath(Exception):
    def __init__(self, file_path):
        message = f'The provided path is empty: {file_path}'
        self.message = message
        super(ErrorEmptyFilePath, self).__init__(self.message)


class DoesNotExistFilePath(Exception):
    def __init__(self, file_path):
        message = f'The provided path does not exist: {file_path}'
        self.message = message
        super(DoesNotExistFilePath, self).__init__(self.message)