class Setuptools:

    @staticmethod
    def match(log):
        return "No module named 'setuptools'" in log


    @staticmethod 
    def apply():
        return (['helpers', 'final'], 'helpers.bi [final.setuptools]')
