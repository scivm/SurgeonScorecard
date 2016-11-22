import os
import sys
from datetime import datetime
import ConfigParser
from pyspark import SparkContext, SparkConf
from pyspark.sql import SQLContext
from pyspark.sql.functions import col,udf, unix_timestamp
from pyspark.sql.types import *
import Model

class Utils:
    def __init__(self, sqlContext):
        self.sqlContext = sqlContext 

    #
    # load a CSV file into a table
    # The name of the table is the same name as the CVS file without extension
    # Compressed files are supported
    #
    def loadCsv(self, sqlContext, file, schema):
        df = self.sqlContext.read.load(file,
                          format='com.databricks.spark.csv',
                          header='true',
                          mode="DROPMALFORMED",
                          schema=schema)
        return df

    # 
    # Write a dataframe to a csv file
    #
    def saveDataframeAsFile(self, df, codec, file):
        df.write.format('com.databricks.spark.csv').options(header="true", codec=codec).save(file)

    #
    # Write a dataframe to a single csv file.  First convert it to pandas dataframe.
    #
    def saveDataframeAsSingleFile(self, df, directory, filename):
        if not os.path.exists(directory):
            os.makedirs(directory)
        dfp = df.toPandas()
        dfp.to_csv(os.path.join(directory,filename), header=True, index=False)

    #
    # generate a table name from the file path
    #
    def getTableNameFromPath(self, file):
        filename_no_ext, file_extension = os.path.splitext(file)
        return os.path.splitext("path_to_file")[0]


    #
    # string the end of string text based on suffix
    #
    def strip_end(self, text, suffix):
        if not text.endswith(suffix):
            return text
        return text[:len(text)-len(suffix)]

    #
    # load data from csv files inside a directory and register as a table
    # reference with a dictionary
    # Handle compression file formats
    #
    def loadRawData(self, sqlContext, directory):
        data = {}
        model = Model.Model()
        for filename in os.listdir(directory):
            key=None
            if filename.lower().endswith('.csv'): key=self.strip_end(filename, ".csv") # no compression
            if filename.lower().endswith('.csv.gz'): key=self.strip_end(filename, ".csv.gz") # gzip
            if filename.lower().endswith('.csv.zip'): key=self.strip_end(filename, ".csv.zip") # zip
            if filename.lower().endswith('.csv.bzip2'): key=self.strip_end(filename, ".csv.bzip2") # bzip2
            if filename.lower().endswith('.csv.lz4'): key=self.strip_end(filename, ".csv.lz4") # lz4
            if filename.lower().endswith('.csv.snappy'): key=self.strip_end(filename, ".csv.snappy") # snappy
            if key != None:
                data[key] = self.loadCsv(sqlContext, os.path.join(directory,filename),model.model_schema[key])
                data[key].registerTempTable(key)  
        return data

    #
    # write data to csv files
    #
    def writeRawData(self, data, codec, directory):
        if not os.path.exists(directory):
            os.makedirs(directory)
        for key, value in data.iteritems():
            self.saveDataframeAsFile(data[key], codec, os.path.join(directory,key))

    #
    # read in the cms icd9 description file CMS32_DESC_LONG_DX.txt into a dictionary
    #
    def readFileIcd9(self, file):
        icd9 = {}
        for line in open(file, 'r'):
            a, sep, b = line.partition(' ')
            b = b.rstrip()
            icd9[a] = b
        return icd9

    #
    #  count the distinct condition_occurrence CONDITION_TYPE_CONCEPT_ID
    #
    def conditionTypeConceptCount(self, sqlContext):
        condition_concept_count = sqlContext.sql("select CONDITION_TYPE_CONCEPT_ID, count(*) COUNT from condition_occurrence group by CONDITION_TYPE_CONCEPT_ID")
        return condition_concept_count

    #
    #  count the distinct procedure_occurrence PROCEDURE_TYPE_CONCEPT_ID
    #
    def procedureTypeConceptCount(self, sqlContext):
        procedure_concept_count = sqlContext.sql("select PROCEDURE_TYPE_CONCEPT_ID, count(*) COUNT from procedure_occurrence group by PROCEDURE_TYPE_CONCEPT_ID")
        return procedure_concept_count

    #
    #  For a particular icd code, count the number of occurrences
    #  This is done by summing the count values in condition_occurrence and procedure_occurrence
    #  Tables condition_occurrence and procedure_occurrence are global
    #
    def icdGrouping(self, sqlContext):
        icd_co = sqlContext.sql("select CONDITION_SOURCE_VALUE SOURCE_VALUE, count(*) COUNT_CO from condition_occurrence group by CONDITION_SOURCE_VALUE")
        icd_po = sqlContext.sql("select PROCEDURE_SOURCE_VALUE SOURCE_VALUE, count(*) COUNT_PO from procedure_occurrence group by PROCEDURE_SOURCE_VALUE")
        icd_all = icd_co.join(icd_po,'SOURCE_VALUE', how='left').fillna(0)
        icd_all = icd_all.withColumn('COUNT', icd_all.COUNT_CO + icd_all.COUNT_PO)
        return icd_all

    #
    #  For a particular icd code, count the number of principal admission diagnosis codes for patients 
    #  undergoing each of the procedures.
    #  Tables condition_occurrence and procedure_occurrence are global
    #
    def icdGroupingPrimary(self, sqlContext):
        icd_co = sqlContext.sql("select CONDITION_SOURCE_VALUE SOURCE_VALUE, count(*) COUNT_CO from condition_occurrence where CONDITION_TYPE_CONCEPT_ID='38000200' group by CONDITION_SOURCE_VALUE")
        icd_po = sqlContext.sql("select PROCEDURE_SOURCE_VALUE SOURCE_VALUE, count(*) COUNT_PO from procedure_occurrence where PROCEDURE_TYPE_CONCEPT_ID='38000251' group by PROCEDURE_SOURCE_VALUE")
        icd_all = icd_co.join(icd_po,'SOURCE_VALUE', how='left').fillna(0)
        icd_all = icd_all.withColumn('COUNT', icd_all.COUNT_CO + icd_all.COUNT_PO)
        return icd_all


    #
    # filter a dataframe by checking a column for a list of codes
    #
    def filterDataframeByCodes(self, df, codes, column):
        df = df.where(df[column].isin(codes))
        return df

    #
    # find persons that have had inpatient stay with a condition_occurrence or procedure_occurrence
    # convert-dates - convert date string columns to date objects
    # OMOP tables are global so do not need to be passed to the function
    #
    def findPersonsWithInpatientStay(self, df, table, date, convert_dates, date_format):
        df.registerTempTable('event_occurrence')
        event = self.strip_end(table, "_occurrence").upper()
        if event == "PROCEDURE":
            start_date = "PROCEDURE_DATE"
            source_value = "PROCEDURE_SOURCE_VALUE"
        else:
            start_date = "CONDITION_START_DATE"
            source_value = "CONDITION_SOURCE_VALUE"
        sqlString = "select distinct event_occurrence.PERSON_ID, visit_occurrence." + date + ", visit_occurrence.PROVIDER_ID, event_occurrence." + source_value+ " SOURCE_VALUE from visit_occurrence join event_occurrence where event_occurrence.PERSON_ID=visit_occurrence.PERSON_ID and event_occurrence." + start_date + " >= visit_occurrence.VISIT_START_DATE and event_occurrence." + start_date + " <= visit_occurrence.VISIT_END_DATE"
        df = self.sqlContext.sql(sqlString)
        if convert_dates:
            date_conversion =  udf (lambda x: datetime.strptime(x, date_format), DateType())
            df = df.withColumn(date, date_conversion(col(date)))
        return df

    #
    # find persons that have been readmitted to the hospital
    # OMOP tables are global so do not need to be passed to the function
    # dates must be date objects
    #
    def findReadmissionPersons(self, inpatient_events, complications, days):
        inpatient_events.registerTempTable('inpatient_events')
        complications.registerTempTable('complications')
        sqlString = "select distinct inpatient_events.PERSON_ID, inpatient_events.VISIT_END_DATE, inpatient_events.PROVIDER_ID, complications.SOURCE_VALUE from inpatient_events join complications where inpatient_events.PERSON_ID=complications.PERSON_ID and inpatient_events.VISIT_END_DATE < complications.VISIT_START_DATE and complications.VISIT_START_DATE < date_add(inpatient_events.VISIT_END_DATE," + days + ")"
        df =  self.sqlContext.sql(sqlString)
        return df

    #
    # count the number of occurrences for a provider
    # OMOP tables are global so do not need to be passed to the function
    #
    def countProviderOccurrence(self, eventDf, sqlContext):
        eventDf.registerTempTable('provider_events')
        provider_event_counts = sqlContext.sql("select provider_events.PROVIDER_ID, count(*) count from provider_events group by PROVIDER_ID order by count desc") 
        return provider_event_counts

    #
    # find counts of icd codes
    # If primary_only flag is set, only count those icd codes designated as primary inpatient codes
    #
    def writeCodesAndCount(self, sqlContext, codes, directory, filename, primary_only):
        if not os.path.exists(directory):
            os.makedirs(directory)
        if primary_only:
            # look only for icd codes that are primary inpatient
            icd_all = self.icdGroupingPrimary(sqlContext).toPandas()
        else:
            # look at all icd codes
            icd_all = self.icdGrouping(sqlContext).toPandas()
        icd_def = self.readFileIcd9('icd/icd9/CMS32_DESC_LONG_DX.txt')  # read icd9 definitions into dict
        f = open(os.path.join(directory,filename), "w")
        total_for_all = 0
        for key, value in codes.iteritems():
            f.write("Key: " + key + "\n")
            f.write("code, count, description\n")
            total = 0
            for code in value:
                if icd_all[icd_all.SOURCE_VALUE==code].empty:
                    icd_count=0
                else:
                    icd_count = icd_all[icd_all.SOURCE_VALUE==code].COUNT.item()
                total += icd_count
                if code not in icd_def:
                    icd_description = ""
                else: 
                    icd_description = icd_def[code]
                outstring = code + "," + str(icd_count) + "," + icd_description + "\n"
                f.write(outstring)
            totalString = "Total Count For This procedure: " + str(total) + "\n\n"
            f.write(totalString)
            total_for_all += total
        totalForAllString = "Total Count For All Procedures: " + str(total_for_all) + "\n"
        f.write(totalForAllString)
        f.close()


    #
    #  For a particular icd code, count the number of occurrences
    #  This is done by summing the count values in condition_occurrence and procedure_occurrence
    #  Tables condition_occurrence and procedure_occurrence are global
    #
    def readmissionGrouping(self, sqlContext, readmission):
        readmission.registerTempTable('readmissioN') 
        icd_count = sqlContext.sql("select SOURCE_VALUE, count(*) COUNT from readmission group by SOURCE_VALUE")
        return icd_count

    #
    # find code counts for readmission event 
    #
    def writeReadmissionCodesAndCount(self, sqlContext, codes, readmissionDfs, directory, filename):
        if not os.path.exists(directory):
            os.makedirs(directory)
        icd_def = self.readFileIcd9('icd/icd9/CMS32_DESC_LONG_DX.txt')  # read icd9 definitions into dict
        f = open(os.path.join(directory,filename), "w")
        total_for_all = 0
        for key, value in codes.iteritems():
            icd_all = self.readmissionGrouping(sqlContext, readmissionDfs[key]).toPandas()
            f.write("Key: " + key + "\n")
            f.write("code, count, description\n")
            total = 0
            for code in value:
                if icd_all[icd_all.SOURCE_VALUE==code].empty:
                    icd_count=0
                else:
                    icd_count = icd_all[icd_all.SOURCE_VALUE==code].COUNT.item()
                total += icd_count
                if code not in icd_def:
                    icd_description = ""
                else:
                    icd_description = icd_def[code]
                outstring = code + "," + str(icd_count) + "," + icd_description + "\n"
                f.write(outstring)
            totalString = "Total Count For This procedure: " + str(total) + "\n\n"
            f.write(totalString)
            total_for_all += total
        totalForAllString = "Total Count For All Procedures: " + str(total_for_all) + "\n"
        f.write(totalForAllString)
        f.close()

